"""Data freshness alert routing rules.

Defines how FEED_UNHEALTHY, DATA_STALE, and DATA_GAP_DETECTED events are
dispatched to notifiers based on event criticality.

Routing matrix
--------------

| Event               | Criticality   | Channel(s)               | SLA           |
|---------------------|---------------|--------------------------|---------------|
| FEED_UNHEALTHY      | critical      | PagerDuty + Slack        | immediate     |
| FEED_UNHEALTHY      | important     | Slack only               | within 60s    |
| FEED_UNHEALTHY      | informational | log only                 | —             |
| DATA_STALE          | critical      | Slack                    | within 60s    |
| DATA_STALE          | important     | Slack                    | within 300s   |
| DATA_STALE          | informational | log only                 | —             |
| DATA_GAP_DETECTED   | any           | Slack (gap > 2x cadence) | within 300s   |

Service event taxonomy:
  SERVICE_EVENT: DATA_FRESHNESS_ALERT_ROUTED
  SERVICE_EVENT: DATA_FRESHNESS_ALERT_FAILED
"""

import logging

from unified_trading_library import log_event

from ..notifiers.pagerduty import PagerDutySeverity
from ..notifiers.pagerduty import send_event as pd_send_event
from ..notifiers.slack import send_message as slack_send_message

logger = logging.getLogger(__name__)

# Freshness event names handled by these rules.
FEED_UNHEALTHY = "FEED_UNHEALTHY"
DATA_STALE = "DATA_STALE"
DATA_GAP_DETECTED = "DATA_GAP_DETECTED"

_FRESHNESS_EVENTS: frozenset[str] = frozenset({FEED_UNHEALTHY, DATA_STALE, DATA_GAP_DETECTED})

# Criticality levels that escalate FEED_UNHEALTHY to PagerDuty.
_PAGERDUTY_CRITICALITIES: frozenset[str] = frozenset({"critical"})

# Criticality levels that suppress Slack (log-only).
_LOG_ONLY_CRITICALITIES: frozenset[str] = frozenset({"informational"})


def _criticality_from_details(details: dict[str, object]) -> str:
    """Extract criticality from event details, defaulting to 'informational'."""
    value = details.get("criticality")
    if isinstance(value, str) and value in {"critical", "important", "informational"}:
        return value
    return "informational"


def _is_significant_gap(details: dict[str, object]) -> bool:
    """Return True when a DATA_GAP_DETECTED event exceeds 2x the expected cadence.

    Uses ``age_seconds`` and ``expected_cadence_seconds`` from event details.
    Falls back to True (always alert) when values are absent.
    """
    age = details.get("age_seconds")
    cadence = details.get("expected_cadence_seconds")
    if not isinstance(age, (int, float)) or not isinstance(cadence, (int, float)):
        return True
    return bool(cadence > 0 and float(age) > 2.0 * float(cadence))


def route_data_freshness_event(event_name: str, details: dict[str, object]) -> None:
    """Route a data freshness event to the correct notifier(s).

    Only handles FEED_UNHEALTHY, DATA_STALE, and DATA_GAP_DETECTED.
    Unknown event names are forwarded to Slack as a safety fallback.

    Args:
        event_name: Canonical event name (one of the three freshness events).
        details: Event details dict; must contain a ``criticality`` key when
                 the event is FEED_UNHEALTHY or DATA_STALE.
    """
    log_event("DATA_FRESHNESS_ALERT_ROUTED", details={"event_name": event_name})

    criticality = _criticality_from_details(details)
    source = str(details.get("source", "alerting-service"))
    message = str(details.get("detail", event_name))
    summary = f"[{event_name}] {message}"

    any_delivery_failed = False

    if event_name == FEED_UNHEALTHY:
        any_delivery_failed = _route_feed_unhealthy(
            criticality=criticality,
            summary=summary,
            source=source,
            details=details,
        )

    elif event_name == DATA_STALE:
        any_delivery_failed = _route_data_stale(
            criticality=criticality,
            summary=summary,
        )

    elif event_name == DATA_GAP_DETECTED:
        any_delivery_failed = _route_data_gap_detected(
            details=details,
            summary=summary,
        )

    else:
        # Unknown freshness-adjacent event — Slack fallback.
        logger.warning("route_data_freshness_event called with unrecognised event=%s", event_name)
        ok = slack_send_message(text=summary, blocks=None)
        if not ok:
            any_delivery_failed = True

    if any_delivery_failed:
        log_event("DATA_FRESHNESS_ALERT_FAILED", details={"event_name": event_name})


def _route_feed_unhealthy(
    criticality: str,
    summary: str,
    source: str,
    details: dict[str, object],
) -> bool:
    """Route a FEED_UNHEALTHY event.

    critical      → PagerDuty + Slack (belt-and-braces)
    important     → Slack only
    informational → log only (no notifier call)

    Returns True when any delivery failed.
    """
    failed = False

    if criticality in _LOG_ONLY_CRITICALITIES:
        logger.info("FEED_UNHEALTHY (informational) logged only: %s", summary)
        return False

    if criticality in _PAGERDUTY_CRITICALITIES:
        severity: PagerDutySeverity = "critical"
        ok = pd_send_event(
            summary=summary,
            severity=severity,
            source=source,
            details=details,
        )
        if not ok:
            logger.error("PagerDuty delivery failed for FEED_UNHEALTHY source=%s", source)
            failed = True

    # Slack for critical (belt-and-braces) and important.
    ok = slack_send_message(text=summary, blocks=None)
    if not ok:
        logger.error("Slack delivery failed for FEED_UNHEALTHY source=%s", source)
        failed = True

    return failed


def _route_data_stale(criticality: str, summary: str) -> bool:
    """Route a DATA_STALE event.

    critical      → Slack (within 60s SLA)
    important     → Slack (within 300s SLA)
    informational → log only

    Returns True when any delivery failed.
    """
    if criticality in _LOG_ONLY_CRITICALITIES:
        logger.info("DATA_STALE (informational) logged only: %s", summary)
        return False

    ok = slack_send_message(text=summary, blocks=None)
    if not ok:
        logger.error("Slack delivery failed for DATA_STALE")
        return True

    return False


def _route_data_gap_detected(details: dict[str, object], summary: str) -> bool:
    """Route a DATA_GAP_DETECTED event.

    Sends to Slack only when the gap exceeds 2x the expected cadence.
    Silently logs and skips when the gap is within the normal cadence window.

    Returns True when delivery was attempted and failed.
    """
    if not _is_significant_gap(details):
        logger.debug("DATA_GAP_DETECTED gap is within 2x cadence — suppressed: %s", summary)
        return False

    ok = slack_send_message(text=summary, blocks=None)
    if not ok:
        logger.error("Slack delivery failed for DATA_GAP_DETECTED")
        return True

    return False
