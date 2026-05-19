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
import time
import uuid
from datetime import UTC, datetime
from fnmatch import fnmatch
from functools import lru_cache
from typing import cast, get_args

from unified_api_contracts import LIVE_ALERT_RULES, AlertRule, KillSwitchScope
from unified_trading_library import (
    classify_and_emit_error,
    get_kill_switch_bus,
    log_event,
)

from ..config import AlertingSystemConfig
from ..config_reloaders import get_paging_credentials
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

# ── Tick-staleness + connectivity-gap coalesce window ────────────────────
# Per alerting_service_live_rules_2026_05_07 § "Tick-staleness +
# connectivity-gap event taxonomy", when both TICK_STALENESS (MDPS) and
# CONNECTIVITY_GAP_DETECTED (MTDS) fire on the same (venue, instrument)
# within a 30-second window, operators see ONE merged alert rather than
# two separate fires. The 30s window keys on ``(venue, instrument)``
# extracted from the alert payload ``details`` dict.
#
# Coalesce state: maps (venue, instrument) → (last_fire_monotonic, last_event_name,
# merged_payload). The first event in the window fires normally + records
# the timestamp; subsequent events within 30s short-circuit return after
# logging a COALESCE_MERGED event with the merged payload (no second fire,
# no second persistence record, no second channel dispatch).
#
# Non-staleness / non-connectivity alerts pass through untouched — the
# coalesce only fires for the closed set ``_COALESCED_EVENT_NAMES``.
_COALESCE_WINDOW_SECONDS: float = 30.0
_COALESCED_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "TICK_STALENESS",
        "CONNECTIVITY_GAP_DETECTED",
    }
)
_coalesce_window: dict[tuple[str, str], tuple[float, str, dict[str, object]]] = {}


# ── Synthetic-event paging suppression (Phase 3.F of simulation_scenarios) ──
# When a scenario harness drives the pipeline via AdversarialMatchingEngine /
# arm_breaker(synthetic=True), downstream alerts carry ``synthetic=True`` in
# their ``details`` payload. The router logs the synthetic alert + persists
# the delivery record (so operator-visible dashboards still see it) but does
# NOT dispatch to PagerDuty / Telegram — paging during synthetic scenario
# drills is unnecessary noise and could confuse on-call.
#
# Per CLAUDE.md "Live = batch" rule, real-fire alerts continue to page
# normally — the ONLY distinction is the ``synthetic`` flag on the payload.

_SYNTHETIC_TRUTHY: frozenset[object] = frozenset({True, "True", "true", "TRUE", "1"})


def _is_synthetic(details: dict[str, object]) -> bool:
    """Return True iff the details payload signals a synthetic scenario fire.

    Per Phase 3.F the producer stamps ``synthetic=True`` (bool) — we accept
    common string serialisations as well so handlers that re-serialise via
    JSON / Pub/Sub envelope conventions still match.
    """
    return details.get("synthetic") in _SYNTHETIC_TRUTHY


def _coalesce_key(event_name: str, details: dict[str, object]) -> tuple[str, str] | None:
    """Extract the (venue, instrument) coalesce key from an alert payload.

    Returns ``None`` if either field is missing — caller MUST pass through
    without coalescing in that case (operator-visible over-broad alert is
    preferred to a silent merge that drops the second fire).
    """
    venue_raw = details.get("venue")
    instrument_raw = details.get("instrument")
    if venue_raw is None or instrument_raw is None:
        return None
    return (str(venue_raw), str(instrument_raw))


def _check_coalesce_window(
    event_name: str,
    details: dict[str, object],
    now_monotonic: float,
) -> bool:
    """Return True if this event should be COALESCED (suppressed); False if it should fire.

    Side-effect: when False (fires normally), records the (key, now, event_name, details)
    entry in ``_coalesce_window`` for future suppression. When True (suppressed),
    merges the new payload into the in-flight entry so the original fire's
    audit trail captures the union of both signals.

    Cleanup: stale entries (>30s old) are evicted on every call to keep
    the dict bounded under high-frequency emission. Eviction is O(N)
    over the window dict, which is bounded by the operator's distinct
    (venue, instrument) set per 30s — small.
    """
    if event_name not in _COALESCED_EVENT_NAMES:
        return False
    key = _coalesce_key(event_name, details)
    if key is None:
        return False
    # Evict stale entries
    for stale_key in [
        k for k, (ts, _, _) in _coalesce_window.items() if now_monotonic - ts >= _COALESCE_WINDOW_SECONDS
    ]:
        del _coalesce_window[stale_key]
    existing = _coalesce_window.get(key)
    if existing is not None and now_monotonic - existing[0] < _COALESCE_WINDOW_SECONDS:
        # Merge new payload into in-flight entry; suppress this fire.
        merged = dict(existing[2])
        merged.update(details)
        merged["_coalesced_with"] = event_name
        _coalesce_window[key] = (existing[0], existing[1], merged)
        return True
    # Record this fire as the in-flight entry; do not suppress.
    _coalesce_window[key] = (now_monotonic, event_name, dict(details))
    return False


def _reset_coalesce_window_for_tests() -> None:  # pyright: ignore[reportUnusedFunction]
    """Test-only helper: clear the coalesce-window dict.

    Used by ``tests/unit/test_router_coalesce.py`` between cases so each
    test starts from a clean window. Not part of the public API."""
    _coalesce_window.clear()


# Module-level GCS store (lazily initialised).
_storage_store_instance: object | None = None

# Batch mode flag — when True, route_event() writes audit records
# instead of delivering to PagerDuty/Telegram/Slack.
# Set from main.py before batch replay starts.
_batch_mode: bool = False

# Batch replay stats — accumulated by route_event() in batch mode.
_batch_would_deliver: dict[str, int] = {}
_batch_deduplicated: int = 0
_batch_matched: int = 0


def set_batch_mode(enabled: bool) -> None:
    """Enable or disable batch delivery suppression."""
    global _batch_mode, _batch_would_deliver, _batch_deduplicated, _batch_matched
    _batch_mode = enabled
    _batch_would_deliver = {}
    _batch_deduplicated = 0
    _batch_matched = 0


def get_batch_stats() -> dict[str, object]:
    """Return accumulated batch replay routing stats."""
    return {
        "would_deliver": dict(_batch_would_deliver),
        "deduplicated": _batch_deduplicated,
        "matched": _batch_matched,
    }


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


def _parse_rule_channels(raw_channels: object) -> set[str]:
    """Parse a routing rule's ``channels`` value into a channel-name set.

    A matched rule with an empty channel list is a LOG_ONLY rule —
    ``AlertRule.to_routing_dict()`` strips ``AlertChannel.LOG_ONLY`` (UAC has no
    legacy "log_only" routing concept), leaving ``[]``. We return the sentinel
    ``{"log_only"}`` so the downstream ``_deliver_to_channels`` distinguishes
    "rule matched → INFO, no delivery" from "no rule matched → telegram fallback".
    """
    channels: set[str] = set()
    if isinstance(raw_channels, list):
        for channel_name in cast("list[object]", raw_channels):
            channels.add(str(channel_name))
    return channels or {"log_only"}


def _parse_rule_severity(severity_raw: object, pattern: str) -> PagerDutySeverity | None:
    """Parse a routing rule's ``severity_filter`` value, defaulting to ``warning``
    on an unrecognised string (with a log warning)."""
    if severity_raw is None:
        return None
    severity_str = str(severity_raw).lower()
    if severity_str in _VALID_SEVERITIES:
        return cast("PagerDutySeverity", severity_str)
    logger.warning(
        "Unknown severity %r in routing rule for %s, defaulting to 'warning'",
        severity_raw,
        pattern,
    )
    return "warning"


def _match_routing_rules(
    event_name: str,
    rules: list[dict[str, object]],
) -> tuple[set[str], PagerDutySeverity | None]:
    """Match event_name against routing rules and return channels + severity.

    Rules are evaluated in order; the FIRST matching rule wins. When no rule
    matches, fall back to telegram only.

    Returns:
        Tuple of (channel_set, pagerduty_severity_or_none).
    """
    for rule in rules:
        pattern = str(rule.get("event_pattern", ""))  # noqa: qg-empty-fallback
        if fnmatch(event_name, pattern):
            channels = _parse_rule_channels(rule.get("channels", []))  # noqa: qg-empty-fallback
            return channels, _parse_rule_severity(rule.get("severity_filter"), pattern)

    # No rule matched — fallback to telegram only
    return {"telegram"}, None


def _is_runtime_alert(event_name: str) -> bool:
    """Return True when event_name matches a specific LIVE_ALERT_RULES entry (runtime ops alert).

    Used by _deliver_message to route to telegram_chat_id_ops when configured.
    Non-LIVE_ALERT_RULES events (CI/QG/internal) use the standard telegram_chat_id.

    The catch-all "*" rule (T4 INFO) is excluded — it exists to ensure nothing fires silently,
    not to mark all events as ops alerts. Only named patterns qualify for ops-channel routing.
    """
    return any(rule.event_pattern != "*" and fnmatch(event_name, rule.event_pattern) for rule in LIVE_ALERT_RULES)


def _deliver_message(event_name: str, summary: str) -> bool:
    """Deliver a message via Telegram (primary) or Slack (deprecated fallback).

    Runtime alerts (matched by LIVE_ALERT_RULES) route to telegram_chat_id_ops when
    configured; CI/QG/internal events use the standard telegram_chat_id.

    SM credentials (hot-reloaded every 300 s by _PagingCredentialsReloader) take
    precedence over env-var values from AlertingSystemConfig when non-empty.

    Returns True if delivery succeeded, False otherwise.
    """
    config = _get_cloud_config()
    sm_creds = get_paging_credentials()

    # SM credentials take precedence over env-var values when non-empty
    bot_token = sm_creds.get("bot_token") or config.telegram_bot_token
    ops_chat_id = sm_creds.get("chat_id_ops") or config.telegram_chat_id_ops
    if ops_chat_id and _is_runtime_alert(event_name):
        chat_id = ops_chat_id
    else:
        chat_id = sm_creds.get("chat_id") or config.telegram_chat_id

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


def _route_synthetic_log_only(
    *,
    event_name: str,
    details: dict[str, object],
    alert_id: str,
    source: str,
) -> None:
    """Synthetic-event short-circuit: log + persist record, NO channel dispatch.

    Phase 3.F (``simulation_scenarios_topology_price_shocks_2026_05_09.md``):
    scenario-fire alerts carry ``synthetic=True`` in their details payload.
    The router emits an ``ALERT_SUPPRESSED_SYNTHETIC`` audit event + persists
    a log-only AlertDeliveryRecord (so operator-visible dashboards still see
    the synthetic alert) but skips PagerDuty + Telegram dispatch.

    Per CLAUDE.md "Live = batch": real-fire alerts still page normally — the
    ONLY distinction from prod alerts is the ``synthetic`` flag on the
    payload.

    Does NOT call ``_get_cloud_config`` / ``_persist_config_snapshot`` — the
    synthetic short-circuit must work in scenario runs that may not have GCP /
    AWS credentials available (CI / local mock runs).
    """
    log_event(
        "ALERT_SUPPRESSED_SYNTHETIC",
        details={
            "event_name": event_name,
            "alert_id": alert_id,
            "source": source,
            "scenario_id": str(details.get("scenario_id", "")),  # noqa: qg-empty-fallback
            "reason": "synthetic=True in alert payload — paging suppressed per Phase 3.F",
        },
    )
    if _batch_mode:
        # Batch mode: record the synthetic dispatch in the per-batch audit log
        # using the LOG_ONLY channel set. Mirrors _record_batch_audit's shape
        # without paging-channel resolution.
        _record_batch_audit(alert_id, event_name, {"log_only"}, None, source, details)
        return
    record = _build_delivery_record(
        alert_id=alert_id,
        channel="log_only",
        status="suppressed_synthetic",
        response_detail="paging suppressed — synthetic=True",
        event_name=event_name,
    )
    _persist_delivery_record(record)


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


def _record_batch_audit(
    alert_id: str,
    event_name: str,
    channels: set[str],
    pd_severity: PagerDutySeverity | None,
    source: str,
    details: dict[str, object],
) -> None:
    """Record what would have been delivered in batch mode (no actual delivery)."""
    global _batch_matched
    _batch_matched += 1
    for ch in channels:
        _batch_would_deliver[ch] = _batch_would_deliver.get(ch, 0) + 1
    _persist_delivery_record(
        {
            "alert_id": alert_id,
            "event_name": event_name,
            "channels": sorted(channels),
            "severity": str(pd_severity) if pd_severity else None,
            "status": "batch_audit",
            "response_detail": "delivery_suppressed_batch_mode",
            "source": source,
            "original_timestamp": str(details.get("timestamp", "")),  # noqa: qg-empty-fallback
            "timestamp": datetime.now(UTC).isoformat(),
            "_batch_replay": True,
        }
    )


def _find_kill_switch_rule(event_name: str) -> AlertRule | None:
    """Return the LIVE_ALERT_RULES entry that matches event_name AND triggers a kill-switch.

    KILL_SWITCH_* AlertCodes carry ``triggers_kill_switch=True`` + a non-None
    ``kill_switch_scope`` per UAC ``alerting_service_live_rules_2026_05_07``
    Phase 1 + the 2026-05-08 publisher-hook migration. Non-kill-switch codes
    return ``None`` (the hook is skipped — channel dispatch still happens).

    Defensive about UAC version-skew: if the running UAC predates the
    ``kill_switch_scope`` field (in-flight ship), we surface a clear log warning
    + return None rather than raising — the alert still pages on-call via the
    normal channel dispatch path.
    """
    for rule in LIVE_ALERT_RULES:
        if not fnmatch(event_name, rule.event_pattern):
            continue
        if not rule.triggers_kill_switch:
            return None
        scope = rule.kill_switch_scope
        if scope is None:
            logger.warning(
                "Kill-switch publisher hook: AlertRule for %s has triggers_kill_switch=True "
                "but kill_switch_scope is None — UAC field not yet shipped or seed not "
                "populated; skipping bus.fire() and routing channels only.",
                event_name,
            )
            return None
        return rule
    return None


def _resolve_scope_key(scope: KillSwitchScope, details: dict[str, object]) -> str | None:
    """Pick a scope_key from the alert payload appropriate for this scope.

    Per ``KillSwitchBus`` semantics, scope_key=None means "wildcard halt all keys
    under this scope" — only intended for GLOBAL. For VENUE / STRATEGY / etc.,
    we extract the corresponding identifier from ``details`` (set by the alert
    emitter); if missing, log + still fire with None (operator-visible
    over-broad halt is safer than no halt).
    """
    if scope is KillSwitchScope.GLOBAL:
        return None
    key_field_by_scope: dict[KillSwitchScope, str] = {
        KillSwitchScope.VENUE: "venue",
        KillSwitchScope.CLIENT: "client_id",
        KillSwitchScope.STRATEGY: "strategy_id",
        KillSwitchScope.ARCHETYPE: "archetype",
        KillSwitchScope.INSTRUMENT: "instrument_id",
    }
    field_name = key_field_by_scope.get(scope)
    if field_name is None:
        return None
    raw_value = details.get(field_name)
    if raw_value is None:
        logger.warning(
            "Kill-switch publisher hook: scope=%s requires details.%s but field "
            "is missing from alert payload; firing with scope_key=None (over-broad halt).",
            scope,
            field_name,
        )
        return None
    return str(raw_value)


def _publish_kill_switch_event(event_name: str, details: dict[str, object], alert_id: str) -> None:
    """Emit a typed KillSwitchEvent to the in-process bus on KILL_SWITCH_* alerts.

    Side-effect-free vs the channel dispatch: if the bus publish raises (e.g. a
    subscriber crashes), the exception is swallowed + logged — the channel
    dispatch must remain reliable. Per the operator directive 2026-05-08, the
    bus subscribers (execution-service / strategy-service / position-balance-monitor)
    own their own retry / circuit-breaker behaviour.

    Reference: ``alerting_service_live_rules_2026_05_07`` Phase 2 → Migrated
    issues 2026-05-08 → "Kill-switch publisher hook."
    """
    rule = _find_kill_switch_rule(event_name)
    if rule is None:
        return
    scope = rule.kill_switch_scope
    if scope is None:
        # Already logged in _find_kill_switch_rule — defensive-double check.
        return
    scope_key = _resolve_scope_key(scope, details)
    reason = str(details.get("message") or details.get("reason") or f"alert {event_name} fired (alert_id={alert_id})")
    try:
        bus = get_kill_switch_bus()
        bus.fire(
            scope,
            scope_key,
            reason=reason,
            fired_by="alerting-service",
        )
        log_event(
            "KILL_SWITCH_PUBLISHED",
            details={
                "event_name": event_name,
                "alert_id": alert_id,
                "scope": scope.value,
                "scope_key": scope_key,
            },
        )
    except Exception as exc:
        # Channel dispatch must never fail because the kill-switch bus failed.
        # Log loud + classify so the operator can diagnose, but DO NOT raise.
        classify_and_emit_error(
            exc,
            service_name="alerting-service",
            operation="publish_kill_switch_event",
        )
        log_event(
            "KILL_SWITCH_PUBLISH_FAILED",
            details={
                "event_name": event_name,
                "alert_id": alert_id,
                "scope": scope.value,
                "scope_key": scope_key,
                "error": str(exc),
            },
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
    global _batch_deduplicated, _batch_matched

    if _deduplicator.is_duplicate(event_name, details):
        logger.debug("Duplicate alert suppressed: %s", event_name)
        if _batch_mode:
            _batch_deduplicated += 1
        return

    # Tick-staleness + connectivity-gap coalesce window — merge concurrent
    # TICK_STALENESS + CONNECTIVITY_GAP_DETECTED fires on the same
    # (venue, instrument) within 30s into ONE operator-visible alert.
    # See ``_check_coalesce_window`` docstring above; non-staleness /
    # non-connectivity events pass through unchanged.
    if _check_coalesce_window(event_name, details, time.monotonic()):
        log_event(
            "ALERT_COALESCED",
            details={
                "event_name": event_name,
                "venue": details.get("venue"),
                "instrument": details.get("instrument"),
                "window_seconds": _COALESCE_WINDOW_SECONDS,
            },
        )
        return

    log_event("ALERT_ROUTED", details={"event_name": event_name})

    alert_id = uuid.uuid4().hex[:16]
    source = str(details.get("source", "alerting-service"))

    # Phase 3.F synthetic-paging-suppression: log + persist + short-circuit
    # BEFORE _get_cloud_config + channel resolution + dispatch when details
    # carries synthetic=True. Avoids touching cloud config during synthetic
    # scenario runs (which may run without GCP/AWS credentials in CI).
    if _is_synthetic(details):
        _route_synthetic_log_only(
            event_name=event_name,
            details=details,
            alert_id=alert_id,
            source=source,
        )
        return

    config = _get_cloud_config()
    summary = f"[{event_name}] {details.get('message', event_name)}"

    # Determine channels from config-driven routing rules
    channels, pd_severity = _match_routing_rules(event_name, config.routing_rules)

    if _batch_mode:
        _record_batch_audit(alert_id, event_name, channels, pd_severity, source, details)
        return

    failed = _deliver_to_channels(
        alert_id,
        event_name,
        summary,
        source,
        channels,
        pd_severity,
        config,
        details,
    )
    status_event = "ALERT_FAILED" if failed else "ALERT_SENT"
    log_event(status_event, details={"event_name": event_name, "alert_id": alert_id})

    # Kill-switch publisher hook — runs AFTER channel dispatch so paging fires
    # even if the bus publish errors. Side-effect-free vs channel dispatch.
    _publish_kill_switch_event(event_name, details, alert_id)

    _persist_config_snapshot(config)


def route_event_with_explicit_channels(
    event_name: str,
    details: dict[str, object],
    *,
    channels: set[str],
    pd_severity: PagerDutySeverity | None,
) -> None:
    """Route an event to an explicitly-supplied channel set, bypassing config rules.

    Used by consumers that compute the severity tier themselves rather than
    relying on a matching ``LIVE_ALERT_RULES`` ``event_pattern`` — e.g. the
    disaster-recovery kill-switch arm/disarm + circuit-breaker fire handlers,
    where the severity is a function of ``KillSwitchId`` scope / ``BreakerAction``
    rather than of a static AlertCode.

    Shares the dedup / persistence / log-event machinery with :func:`route_event`;
    the ONLY difference is that channel resolution is supplied by the caller.
    ``channels == {"log_only"}`` (or any set excluding pagerduty/telegram) results
    in no delivery — only the ``ALERT_ROUTED`` / ``ALERT_SENT`` audit trail.
    """
    global _batch_deduplicated

    if _deduplicator.is_duplicate(event_name, details):
        logger.debug("Duplicate alert suppressed (explicit channels): %s", event_name)
        if _batch_mode:
            _batch_deduplicated += 1
        return

    log_event("ALERT_ROUTED", details={"event_name": event_name})

    alert_id = uuid.uuid4().hex[:16]
    source = str(details.get("source", "alerting-service"))

    # Phase 3.F synthetic-paging-suppression: log + persist + short-circuit
    # BEFORE _get_cloud_config + channel dispatch when details carries
    # synthetic=True.
    if _is_synthetic(details):
        _route_synthetic_log_only(
            event_name=event_name,
            details=details,
            alert_id=alert_id,
            source=source,
        )
        return

    config = _get_cloud_config()
    summary = f"[{event_name}] {details.get('message', event_name)}"

    if _batch_mode:
        _record_batch_audit(alert_id, event_name, channels, pd_severity, source, details)
        return

    failed = _deliver_to_channels(
        alert_id,
        event_name,
        summary,
        source,
        channels,
        pd_severity,
        config,
        details,
    )
    status_event = "ALERT_FAILED" if failed else "ALERT_SENT"
    log_event(status_event, details={"event_name": event_name, "alert_id": alert_id})
    _persist_config_snapshot(config)


def _deliver_to_channels(
    alert_id: str,
    event_name: str,
    summary: str,
    source: str,
    channels: set[str],
    pd_severity: PagerDutySeverity | None,
    config: AlertingSystemConfig,
    details: dict[str, object],
) -> bool:
    """Deliver to all matched channels. Returns True if any delivery failed."""
    any_failed = False

    pd_suppressed = config.pagerduty_disabled or config.quietness_baseline_mode
    if "pagerduty" in channels and not pd_suppressed:
        severity: PagerDutySeverity = pd_severity or "critical"
        ok = pd_send_event(summary=summary, severity=severity, source=source, details=details)
        if not ok:
            logger.error("PagerDuty delivery failed for event %s", event_name)
            any_failed = True
        _persist_delivery_record(
            _build_delivery_record(
                alert_id,
                "pagerduty",
                "sent" if ok else "failed",
                "accepted" if ok else "delivery_failed",
                event_name,
            )
        )
    elif "pagerduty" in channels and pd_suppressed:
        logger.info("PagerDuty suppressed (quietness_baseline_mode/pagerduty_disabled) for event %s", event_name)

    if "telegram" in channels or not channels:
        ok = _deliver_message(event_name, summary)
        channel_used = "telegram" if config.telegram_bot_token else "slack"
        if not ok:
            any_failed = True
        _persist_delivery_record(
            _build_delivery_record(
                alert_id,
                channel_used,
                "sent" if ok else "failed",
                "accepted" if ok else "delivery_failed",
                event_name,
            )
        )

    return any_failed
