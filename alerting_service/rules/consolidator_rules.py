"""Manifest-consolidator liveness + consolidation-failure alert rules.

Consumes the two UTL consolidator events that previously had NO consumer in
alerting-service (manifest_reader_fail_fast_on_stale_fallback_2026_05_28
follow-up — a crash-looping or down consolidator paged no one):

- ``CONSOLIDATOR_DOWN`` — emitted by the consolidator liveness watchdog
  (``unified_trading_library.monitors.consolidator_liveness``) when a bucket's
  consolidation heartbeat is stale while per-VM shards exist. The consolidator
  is the SSOT freshness mechanism the whole data-status stack depends on —
  when it is down every manifest reader is one stale-index away from
  loud-fail (``ManifestConsolidatorStaleError``). → **CRITICAL**
  (PagerDuty + Telegram, page now).

- ``MANIFEST_CONSOLIDATION_FAILED`` — emitted by the manifest consolidator
  itself when a consolidation cycle fails. A single failed cycle usually
  self-heals on the next ``*/1`` Scheduler tick → **WARN** (Telegram only).
  Repeated failures inside the sliding window mean the consolidator is
  crash-looping → escalate to **CRITICAL** (PagerDuty + Telegram). The
  repeat detection reuses the service's existing ``CircuitBreaker``
  sliding-window/threshold idiom (same machinery ``error_event_handler``
  uses for SERVICE_ERROR rate tracking) keyed per manifest bucket.

Severity → channel mapping mirrors ``dr_event_handler._severity_channels``:
CRITICAL → PagerDuty(critical) + Telegram; WARN → Telegram only.

Service event taxonomy:
  SERVICE_EVENT: CONSOLIDATOR_ALERT_ROUTED
"""

from __future__ import annotations

import logging

from unified_api_contracts import AlertSeverity
from unified_trading_library import log_event

# Event-name strings mirror the UTL emitters (SSOT:
# unified_trading_library/events/event_types.py — CONSOLIDATOR_DOWN :689,
# MANIFEST_CONSOLIDATION_FAILED :682). Matched by NAME like the sibling rule
# modules: the constants are not re-exported on the UTL facade and the
# import-pattern gate forbids deep `unified_trading_library.events` imports.
CONSOLIDATOR_DOWN = "CONSOLIDATOR_DOWN"
MANIFEST_CONSOLIDATION_FAILED = "MANIFEST_CONSOLIDATION_FAILED"

from ..circuit_breaker import STATE_OPEN, CircuitBreaker
from ..notifiers.pagerduty import PagerDutySeverity
from ..notifiers.router import route_event_with_explicit_channels

logger = logging.getLogger(__name__)

_PAGING_CHANNELS: frozenset[str] = frozenset({"pagerduty", "telegram"})
_CRITICAL: PagerDutySeverity = "critical"

_CONSOLIDATOR_EVENTS: frozenset[str] = frozenset({CONSOLIDATOR_DOWN, MANIFEST_CONSOLIDATION_FAILED})

# Repeat-failure escalation breaker (reuses the service CircuitBreaker idiom —
# do NOT invent new machinery). The consolidator runs on a */1 Scheduler tick,
# so a single MANIFEST_CONSOLIDATION_FAILED is usually healed by the next
# cycle. 3 failures inside a 30-minute sliding window (~30 cycles) keyed on
# the same bucket = a crash-looping consolidator → the circuit opens and the
# alert escalates WARN → CRITICAL. While the circuit stays OPEN, subsequent
# failures keep paging at CRITICAL; cooldown lets it probe HALF_OPEN after
# 30 minutes of quiet so a healed consolidator de-escalates.
_BREAKER_SERVICE_KEY = "manifest-consolidator"
_consolidation_failure_breaker = CircuitBreaker(
    window_seconds=1800.0,
    threshold=3,
    cooldown_seconds=1800.0,
)


def get_consolidation_failure_breaker() -> CircuitBreaker:
    """Return the module-level repeat-failure breaker.

    Exposed for testing and health-check endpoints (mirrors
    ``error_event_handler.get_circuit_breaker``).
    """
    return _consolidation_failure_breaker


def _severity_channels(severity: AlertSeverity) -> tuple[set[str], PagerDutySeverity | None]:
    """Return (channel_set, pagerduty_severity) for an ``AlertSeverity``.

    CRITICAL → page (PagerDuty critical + Telegram); everything else this
    module emits is WARN → Telegram only.
    """
    if severity is AlertSeverity.CRITICAL:
        return set(_PAGING_CHANNELS), _CRITICAL
    return {"telegram"}, None


def _enrich(details: dict[str, object], severity: AlertSeverity, default_message: str) -> dict[str, object]:
    """Build the route_event details payload from the raw event payload."""
    return {
        **details,
        "message": str(details.get("detail") or details.get("message") or default_message),
        "severity": severity.value,
        "source": str(details.get("source", "alerting-service/consolidator-rules")),
    }


def handle_consolidator_down_payload(payload: dict[str, object]) -> None:
    """Route a CONSOLIDATOR_DOWN event at CRITICAL severity (page now).

    Payload keys (from ``ConsolidatorLivenessMonitor.check_and_emit``):
    ``bucket``, ``heartbeat_age_sec``, ``max_age_sec``, ``detail``.
    """
    bucket = str(payload.get("bucket", "unknown"))
    severity = AlertSeverity.CRITICAL
    channels, pd_severity = _severity_channels(severity)
    log_event(
        "CONSOLIDATOR_ALERT_ROUTED",
        details={
            "event_name": CONSOLIDATOR_DOWN,
            "bucket": bucket,
            "severity": severity.value,
        },
    )
    logger.error(
        "Consolidator DOWN for bucket=%s heartbeat_age_sec=%s — every manifest "
        "reader on this bucket is one stale-index away from loud-fail",
        bucket,
        payload.get("heartbeat_age_sec"),
    )
    enriched = _enrich(
        payload,
        severity,
        default_message=f"manifest consolidator DOWN for bucket={bucket}",
    )
    route_event_with_explicit_channels(
        CONSOLIDATOR_DOWN,
        enriched,
        channels=channels,
        pd_severity=pd_severity,
    )


def handle_manifest_consolidation_failed_payload(payload: dict[str, object]) -> None:
    """Route a MANIFEST_CONSOLIDATION_FAILED event — WARN, escalating to
    CRITICAL on repeated failures for the same bucket within the breaker window.

    Payload keys (from ``manifest_consolidator``): ``bucket``, ``error`` /
    ``detail`` context fields.
    """
    bucket = str(payload.get("bucket", "unknown"))
    breaker = get_consolidation_failure_breaker()
    transition = breaker.record_error(_BREAKER_SERVICE_KEY, bucket)
    circuit_open = transition == STATE_OPEN or breaker.get_state(_BREAKER_SERVICE_KEY, bucket) == STATE_OPEN
    severity = AlertSeverity.CRITICAL if circuit_open else AlertSeverity.WARN
    channels, pd_severity = _severity_channels(severity)
    failure_count = breaker.get_error_count(_BREAKER_SERVICE_KEY, bucket)
    log_event(
        "CONSOLIDATOR_ALERT_ROUTED",
        details={
            "event_name": MANIFEST_CONSOLIDATION_FAILED,
            "bucket": bucket,
            "severity": severity.value,
            "failures_in_window": failure_count,
        },
    )
    logger.warning(
        "Manifest consolidation FAILED for bucket=%s (failures_in_window=%d, severity=%s)",
        bucket,
        failure_count,
        severity.value,
    )
    enriched = _enrich(
        payload,
        severity,
        default_message=f"manifest consolidation cycle failed for bucket={bucket}",
    )
    enriched["failures_in_window"] = failure_count
    if circuit_open:
        enriched["escalation_reason"] = (
            f"{failure_count} consolidation failures within "
            f"{breaker.window:.0f}s for bucket={bucket} — consolidator is crash-looping"
        )
    route_event_with_explicit_channels(
        MANIFEST_CONSOLIDATION_FAILED,
        enriched,
        channels=channels,
        pd_severity=pd_severity,
    )


def route_consolidator_event(event_name: str, details: dict[str, object]) -> None:
    """Dispatch a consolidator event to the matching handler.

    Only handles CONSOLIDATOR_DOWN and MANIFEST_CONSOLIDATION_FAILED; an
    unknown event name is logged and produces NO alert (closed-set rule —
    the generic router fallback already covers everything else).
    """
    if event_name == CONSOLIDATOR_DOWN:
        handle_consolidator_down_payload(details)
        return
    if event_name == MANIFEST_CONSOLIDATION_FAILED:
        handle_manifest_consolidation_failed_payload(details)
        return
    logger.warning("route_consolidator_event called with unrecognised event=%s", event_name)
