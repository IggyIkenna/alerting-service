"""
Event router: directs events to the appropriate notifier(s).

Routing is config-driven via ``AlertingSystemConfig.routing_rules``.
Each rule specifies an ``event_pattern`` (fnmatch glob), a list of
``channels``, and an optional ``severity_filter``.

Default rules (when no custom config is provided):
- KILL_SWITCH_*          -> PagerDuty (critical) AND Telegram
- CIRCUIT_BREAKER_OPEN   -> PagerDuty (critical) AND Telegram
- PREFLIGHT_FAILED       -> Telegram
- SERVICE_DEGRADED       -> Telegram
- All other events       -> Telegram (operational fallback)

Service event taxonomy (for observability and test compliance):
  SERVICE_EVENT: ALERT_SENT
  SERVICE_EVENT: ALERT_FAILED
  SERVICE_EVENT: KILL_SWITCH_ACTIVATED
  SERVICE_EVENT: KILL_SWITCH_DEACTIVATED
  SERVICE_EVENT: CIRCUIT_BREAKER_OPEN
  SERVICE_EVENT: PREFLIGHT_FAILED
"""

import logging
import uuid
from datetime import UTC, datetime
from fnmatch import fnmatch
from functools import lru_cache
from typing import cast, get_args

from unified_events_interface import log_event
from unified_trading_library import classify_and_emit_error

from ..config import AlertingSystemConfig
from ..core.dedup import AlertDeduplicator
from ..persistence.storage_store import AlertStorageStore
from .pagerduty import PagerDutySeverity
from .pagerduty import send_event as pd_send_event
from .slack import send_message as slack_send_message  # DEPRECATED: use Telegram
from .telegram import send_telegram

logger = logging.getLogger(__name__)

_VALID_SEVERITIES: frozenset[str] = frozenset(get_args(PagerDutySeverity))

# Module-level deduplicator (shared across all route_event calls).
_deduplicator = AlertDeduplicator(ttl_seconds=60.0)

# Module-level GCS store (lazily initialised).
_storage_store_instance: object | None = None


@lru_cache(maxsize=1)
def _get_cloud_config() -> AlertingSystemConfig:
    """Return singleton AlertingSystemConfig instance."""
    return AlertingSystemConfig()


def _get_storage_store() -> AlertStorageStore:
    """Return singleton AlertStorageStore instance."""
    global _storage_store_instance
    if _storage_store_instance is None:
        _storage_store_instance = AlertStorageStore()
    return cast("AlertStorageStore", _storage_store_instance)


def _match_routing_rules(
    event_name: str,
    rules: list[dict[str, object]],
) -> tuple[set[str], PagerDutySeverity | None]:
    """Match event_name against routing rules and return channels + severity.

    Rules are evaluated in order; the FIRST matching rule wins.

    Returns:
        Tuple of (channel_set, pagerduty_severity_or_none).
    """
    for rule in rules:
        pattern = str(rule.get("event_pattern", ""))  # noqa: qg-empty-fallback
        if fnmatch(event_name, pattern):
            raw_channels = rule.get("channels", [])  # noqa: qg-empty-fallback
            channels: set[str] = set()
            if isinstance(raw_channels, list):
                for channel_name in cast("list[object]", raw_channels):
                    channels.add(str(channel_name))
            severity_raw = rule.get("severity_filter")
            severity: PagerDutySeverity | None = None
            if severity_raw is not None:
                severity_str = str(severity_raw).lower()
                if severity_str in _VALID_SEVERITIES:
                    severity = cast("PagerDutySeverity", severity_str)
                else:
                    logger.warning(
                        "Unknown severity %r in routing rule for %s, defaulting to 'warning'",
                        severity_raw,
                        pattern,
                    )
                    severity = "warning"
            return channels, severity

    # No rule matched — fallback to telegram only
    return {"telegram"}, None


def _deliver_message(event_name: str, summary: str) -> bool:
    """Deliver a message via Telegram (primary) or Slack (deprecated fallback).

    Returns True if delivery succeeded, False otherwise.
    """
    config = _get_cloud_config()
    bot_token = config.telegram_bot_token
    chat_id = config.telegram_chat_id

    if bot_token and chat_id:
        ok = send_telegram(message=summary, bot_token=bot_token, chat_id=chat_id)
        if not ok:
            logger.error("Telegram delivery failed for event %s", event_name)
        return ok

    # DEPRECATED: Slack fallback when Telegram is not configured
    logger.warning("Telegram not configured — falling back to Slack for %s", event_name)
    ok = slack_send_message(text=summary, blocks=None)
    if not ok:
        logger.error("Slack delivery failed for event %s", event_name)
    return ok


def _build_delivery_record(
    alert_id: str,
    channel: str,
    status: str,
    response_detail: str,
    event_name: str,
) -> dict[str, object]:
    """Build an AlertDeliveryRecord dict."""
    return {
        "alert_id": alert_id,
        "channel": channel,
        "status": status,
        "response_detail": response_detail,
        "event_name": event_name,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _persist_delivery_record(record: dict[str, object]) -> None:
    """Write a delivery record to GCS history (best-effort)."""
    try:
        store = _get_storage_store()
        store.write_alert_history(record)
    except Exception as exc:
        classify_and_emit_error(
            exc,
            service_name="alerting-service",
            operation="persist_delivery_record",
        )


def _persist_config_snapshot(config: AlertingSystemConfig) -> None:
    """Write current routing config to GCS configs/ (best-effort)."""
    try:
        store = _get_storage_store()
        snapshot: dict[str, object] = {"routing_rules": config.routing_rules}
        store.write_config_snapshot(snapshot, name="routing_rules")
    except Exception as exc:
        classify_and_emit_error(
            exc,
            service_name="alerting-service",
            operation="persist_config_snapshot",
        )


def route_event(event_name: str, details: dict[str, object]) -> None:
    """Route an event to the correct notifier(s) based on config-driven rules.

    Deduplication: events with the same name and details hash are
    suppressed for 60 seconds.

    Routing rules are read from ``AlertingSystemConfig.routing_rules``.
    The first matching rule (by fnmatch glob) determines which channels
    receive the event.

    Each delivery attempt produces an ``AlertDeliveryRecord`` persisted
    to GCS history.

    Args:
        event_name: Canonical event name (e.g. "KILL_SWITCH_ACTIVATED").
        details: Arbitrary context dictionary forwarded to the notifier payload.
    """
    if _deduplicator.is_duplicate(event_name, details):
        logger.debug("Duplicate alert suppressed: %s", event_name)
        return

    log_event("ALERT_ROUTED", details={"event_name": event_name})

    config = _get_cloud_config()
    alert_id = uuid.uuid4().hex[:16]
    summary = f"[{event_name}] {details.get('message', event_name)}"
    source = str(details.get("source", "alerting-service"))

    # Determine channels from config-driven routing rules
    channels, pd_severity = _match_routing_rules(event_name, config.routing_rules)

    any_delivery_failed = False

    # PagerDuty delivery
    if "pagerduty" in channels:
        severity: PagerDutySeverity = pd_severity or "critical"
        ok = pd_send_event(
            summary=summary,
            severity=severity,
            source=source,
            details=details,
        )
        status = "sent" if ok else "failed"
        if not ok:
            logger.error("PagerDuty delivery failed for event %s", event_name)
            any_delivery_failed = True
        _persist_delivery_record(
            _build_delivery_record(
                alert_id=alert_id,
                channel="pagerduty",
                status=status,
                response_detail="accepted" if ok else "delivery_failed",
                event_name=event_name,
            )
        )

    # Telegram (primary) or Slack (deprecated fallback)
    if "telegram" in channels or not channels:
        ok = _deliver_message(event_name, summary)
        channel_used = "telegram" if config.telegram_bot_token else "slack"
        status = "sent" if ok else "failed"
        if not ok:
            any_delivery_failed = True
        _persist_delivery_record(
            _build_delivery_record(
                alert_id=alert_id,
                channel=channel_used,
                status=status,
                response_detail="accepted" if ok else "delivery_failed",
                event_name=event_name,
            )
        )

    status_event = "ALERT_FAILED" if any_delivery_failed else "ALERT_SENT"
    log_event(status_event, details={"event_name": event_name, "alert_id": alert_id})

    # Persist config snapshot (best-effort, async-safe)
    _persist_config_snapshot(config)
