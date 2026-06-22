"""Data-pipeline alerts → Slack mirror notifier.

Data-pipeline self-monitoring alerts (the ``DP_*`` family + ``CONSOLIDATOR_DOWN``,
matched by UAC ``DATA_PIPELINE_ALERT_RULES``) are mirrored to the
``#data-pipeline-alerts`` Slack channel so operators watching Slack see the
pipeline's honest-absence gate trips, VM exit-code failures, non-canonical
writes, rate-limit stalls, etc.

The webhook belongs to a dedicated ``data-pipeline-alerts`` Slack app/channel —
distinct from the ``#uts-live-alerts`` mirror (``uts_live_alerts_slack.py``).
The webhook is hot-reloaded from Secret Manager
(``DATA_PIPELINE_ALERTS_SLACK_WEBHOOK``) via ``config_reloaders.py``.

Delivery is best-effort and never raises: when no webhook is configured (mock /
CI / not-yet-provisioned), the notifier no-ops. This is the start-verbose
channel of `data_pipeline_hardening_self_monitoring_2026_06_22.md` Phase 0/2;
CRITICAL events ALSO route through the existing incident path (PagerDuty +
Telegram) — this notifier is the channel mirror, not the only sink.
"""

from __future__ import annotations

import logging
import time

import httpx
from unified_trading_library import log_event

logger = logging.getLogger(__name__)

# Pre-sleep delays for the 2nd and 3rd attempts (no sleep before the 1st).
# Mirrors notifiers/uts_live_alerts_slack.py retry semantics.
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
    if "UNPROVEN" in upper or "EXIT_NONZERO" in upper or "NONCANONICAL" in upper:
        return ":rotating_light:"
    if "FAILED" in upper or "ERROR" in upper or "DOWN" in upper:
        return ":x:"
    return ":warning:"


def _build_blocks(event_name: str, summary: str, details: dict[str, object]) -> list[dict[str, object]]:
    """Build a Block Kit payload mirroring the data-pipeline alert style."""
    severity_raw = details.get("severity")
    severity = str(severity_raw) if severity_raw is not None else None
    emoji = _emoji_for(event_name, severity)

    fields: list[dict[str, object]] = [
        {"type": "mrkdwn", "text": f"*Event:*\n`{event_name}`"},
    ]
    if severity is not None:
        fields.append({"type": "mrkdwn", "text": f"*Severity:*\n{severity}"})
    for label, key in (
        ("Asset group", "asset_group"),
        ("Data type", "data_type"),
        ("Source", "source"),
        ("Venue", "venue"),
        ("Bucket", "bucket"),
        ("VM", "vm"),
        ("Category", "category"),
    ):
        value = details.get(key)
        if value:
            fields.append({"type": "mrkdwn", "text": f"*{label}:*\n{value}"})

    blocks: list[dict[str, object]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Data-Pipeline Alert"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "section", "fields": fields},
    ]
    return blocks


def send_data_pipeline_alert(
    webhook_url: str,
    event_name: str,
    summary: str,
    details: dict[str, object],
) -> bool:
    """Mirror a data-pipeline alert to the #data-pipeline-alerts Slack channel.

    Args:
        webhook_url: Incoming Webhook URL for #data-pipeline-alerts. Empty → no-op.
        event_name: Canonical DP_* event name (e.g. "DP_UNPROVEN_HONEST_ABSENCE").
        summary: Pre-formatted one-line alert summary.
        details: Alert payload — used to enrich the Block Kit fields (asset_group,
            data_type, source, venue, bucket, vm, severity).

    Returns:
        True when Slack accepted the message (HTTP 2xx); False on no-op or failure.
        Never raises — the data-pipeline gate / VM / watcher must not depend on
        this mirror, and CRITICAL events page separately via the incident path.
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
            logger.warning("data-pipeline-alerts Slack mirror failed (attempt %d): %s", attempt + 1, exc)
            continue
        if response.status_code < 300:
            log_event(
                "SLACK_MESSAGE_SENT",
                details={"event_name": event_name, "channel": "data-pipeline-alerts"},
            )
            return True
        if response.status_code < 500:
            # 4xx — bad webhook / payload; retrying will not help.
            logger.error(
                "data-pipeline-alerts Slack webhook rejected (status %d): %s",
                response.status_code,
                response.text,
            )
            return False
        logger.warning(
            "data-pipeline-alerts Slack webhook 5xx (status %d, attempt %d)",
            response.status_code,
            attempt + 1,
        )

    logger.error("data-pipeline-alerts Slack mirror exhausted retries for event %s", event_name)
    return False
