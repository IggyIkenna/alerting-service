"""UTS Live Alerts → Slack mirror notifier.

Live-ops runtime alerts (matched by ``LIVE_ALERT_RULES``) are delivered to the
Telegram "UTS Live Alerts" group. This notifier mirrors those same alerts to
the ``#uts-live-alerts`` Slack channel so operators who watch Slack see them
too.

The webhook belongs to the same ``agent-orchestrator-alerts`` Slack app used by
``agent-orchestrator/server/notifications/slack.py`` — same credentials, a
distinct Incoming Webhook bound to the ``#uts-live-alerts`` channel.

Delivery is best-effort and never raises: when no webhook is configured (mock /
CI / not-yet-provisioned), the notifier no-ops. Telegram remains the primary
delivery channel for live alerts; this is a parallel mirror, not a fallback.
"""

from __future__ import annotations

import logging
import time

import httpx
from unified_trading_library import log_event

logger = logging.getLogger(__name__)

# Pre-sleep delays for the 2nd and 3rd attempts (no sleep before the 1st).
# Mirrors agent-orchestrator/server/notifications/slack.py retry semantics.
_BACKOFF_SECS: tuple[float, ...] = (0.5, 1.0)

_SEVERITY_EMOJI: dict[str, str] = {
    "critical": ":rotating_light:",
    "error": ":x:",
    "high": ":x:",
    "warning": ":warning:",
    "warn": ":warning:",
    "info": ":information_source:",
}


def _emoji_for(event_name: str, severity: str | None) -> str:
    """Pick a Slack emoji from severity, falling back to event-name heuristics."""
    if severity is not None:
        emoji = _SEVERITY_EMOJI.get(severity.lower())
        if emoji is not None:
            return emoji
    upper = event_name.upper()
    if "KILL_SWITCH" in upper or "CIRCUIT_BREAKER" in upper:
        return ":rotating_light:"
    if "FAILED" in upper or "ERROR" in upper:
        return ":x:"
    return ":warning:"


def _build_blocks(event_name: str, summary: str, details: dict[str, object]) -> list[dict[str, object]]:
    """Build a Block Kit payload mirroring the agent-orchestrator alert style."""
    severity_raw = details.get("severity")
    severity = str(severity_raw) if severity_raw is not None else None
    emoji = _emoji_for(event_name, severity)

    fields: list[dict[str, object]] = [
        {"type": "mrkdwn", "text": f"*Event:*\n`{event_name}`"},
    ]
    if severity is not None:
        fields.append({"type": "mrkdwn", "text": f"*Severity:*\n{severity}"})
    for label, key in (
        ("Service", "service"),
        ("Venue", "venue"),
        ("Strategy", "strategy_id"),
        ("Client", "client_id"),
        ("Environment", "environment"),
    ):
        value = details.get(key)
        if value:
            fields.append({"type": "mrkdwn", "text": f"*{label}:*\n{value}"})

    blocks: list[dict[str, object]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} UTS Live Alert"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "section", "fields": fields},
    ]
    # PagerDuty SHADOW annotation (calibration, 2026-06-23): while PagerDuty is held
    # off (PAGERDUTY_DISABLED), each Slack alert states how it WOULD escalate to PD so
    # the operator can calibrate PD routing/severity before enabling it. The caller
    # (router._deliver_to_channels) computes it from the matched channels + pd_severity
    # and threads it through ``details["_pagerduty_shadow"]``.
    shadow = details.get("_pagerduty_shadow")
    if shadow:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f":pager: *PagerDuty escalation:* {shadow}"}]}
        )
    return blocks


def send_uts_live_alert(
    webhook_url: str,
    event_name: str,
    summary: str,
    details: dict[str, object],
) -> bool:
    """Mirror a live-ops alert to the #uts-live-alerts Slack channel.

    Args:
        webhook_url: Incoming Webhook URL for #uts-live-alerts. Empty → no-op.
        event_name: Canonical event name (e.g. "KILL_SWITCH_ACTIVATED").
        summary: Pre-formatted one-line alert summary (matches Telegram body).
        details: Alert payload — used to enrich the Block Kit fields.

    Returns:
        True when Slack accepted the message (HTTP 2xx); False on no-op or failure.
        Never raises — live-alert Telegram delivery must not depend on this mirror.
    """
    if not webhook_url:
        return False

    payload: dict[str, object] = {
        "text": summary,
        "blocks": _build_blocks(event_name, summary, details),
    }

    for attempt in range(3):
        if attempt > 0:
            time.sleep(_BACKOFF_SECS[attempt - 1])
        try:
            response = httpx.post(webhook_url, json=payload, timeout=5.0)
        except httpx.HTTPError as exc:
            logger.warning("UTS-live-alerts Slack mirror failed (attempt %d): %s", attempt + 1, exc)
            continue
        if response.status_code < 300:
            log_event(
                "SLACK_MESSAGE_SENT",
                details={"event_name": event_name, "channel": "uts-live-alerts"},
            )
            return True
        if response.status_code < 500:
            # 4xx — bad webhook / payload; retrying will not help.
            logger.error(
                "UTS-live-alerts Slack webhook rejected (status %d): %s",
                response.status_code,
                response.text,
            )
            return False
        logger.warning(
            "UTS-live-alerts Slack webhook 5xx (status %d, attempt %d)",
            response.status_code,
            attempt + 1,
        )

    logger.error("UTS-live-alerts Slack mirror exhausted retries for event %s", event_name)
    return False
