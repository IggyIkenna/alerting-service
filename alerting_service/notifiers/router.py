"""
Event router: directs events to the appropriate notifier(s).

Routing rules:
- KILL_SWITCH_ACTIVATED  → PagerDuty (critical) AND Slack (belt and braces)
- CIRCUIT_BREAKER_OPEN   → PagerDuty (critical)
- PREFLIGHT_FAILED       → Slack
- SERVICE_DEGRADED       → Slack
- All other events       → Slack (operational fallback)
"""

import logging

from unified_events_interface import log_event

from .pagerduty import PagerDutySeverity
from .pagerduty import send_event as pd_send_event
from .slack import send_message as slack_send_message

logger = logging.getLogger(__name__)

# Events that must page the on-call engineer immediately.
# Values are constrained to PagerDutySeverity literals.
_PAGERDUTY_EVENTS: dict[str, PagerDutySeverity] = {
    "KILL_SWITCH_ACTIVATED": "critical",
    "CIRCUIT_BREAKER_OPEN": "critical",
}

# Events that also require Slack notification regardless of PagerDuty routing.
_ALWAYS_SLACK_EVENTS: frozenset[str] = frozenset({"KILL_SWITCH_ACTIVATED"})

# Events routed exclusively to Slack (operational / pipeline).
_SLACK_ONLY_EVENTS: frozenset[str] = frozenset({"PREFLIGHT_FAILED", "SERVICE_DEGRADED"})


def route_event(event_name: str, details: dict[str, object]) -> None:
    """Route an event to the correct notifier(s).

    KILL_SWITCH_ACTIVATED is sent to both PagerDuty and Slack.
    CIRCUIT_BREAKER_OPEN is sent to PagerDuty only.
    PREFLIGHT_FAILED and SERVICE_DEGRADED are sent to Slack only.
    Any other event name falls back to Slack.

    Args:
        event_name: Canonical event name (e.g. "KILL_SWITCH_ACTIVATED").
        details: Arbitrary context dictionary forwarded to the notifier payload.
    """
    log_event("ALERT_ROUTED", details={"event_name": event_name})

    summary = f"[{event_name}] {details.get('message', event_name)}"
    source = str(details.get("source", "alerting-service"))

    sent_to_pagerduty = False

    if event_name in _PAGERDUTY_EVENTS:
        severity: PagerDutySeverity = _PAGERDUTY_EVENTS[event_name]
        ok = pd_send_event(
            summary=summary,
            severity=severity,
            source=source,
            details=details,
        )
        if not ok:
            logger.error("PagerDuty delivery failed for event %s", event_name)
        sent_to_pagerduty = True

    if (
        event_name in _SLACK_ONLY_EVENTS
        or event_name in _ALWAYS_SLACK_EVENTS
        or not sent_to_pagerduty
    ):
        ok = slack_send_message(text=summary, blocks=None)
        if not ok:
            logger.error("Slack delivery failed for event %s", event_name)
