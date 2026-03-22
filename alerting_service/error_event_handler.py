"""Handler for SERVICE_ERROR lifecycle events.

Processes SERVICE_ERROR events emitted by classify_and_emit_error() across
all services. Each error is:

1. Parsed into an EnhancedError (if the payload contains one)
2. Routed to the appropriate alert channel by severity
3. Fed to the circuit breaker for error-rate tracking
4. If the circuit opens -> emits a CIRCUIT_OPEN lifecycle event

This module owns the singleton CircuitBreaker instance used by the
alerting-service.
"""

from __future__ import annotations

import logging

from unified_events_interface import log_event
from unified_internal_contracts import EnhancedError, LifecycleEventType

from .circuit_breaker import CircuitBreaker
from .metrics import CIRCUIT_STATE_TRANSITIONS, CIRCUITS_OPEN, SERVICE_ERRORS_TOTAL
from .notifiers.router import route_event

logger = logging.getLogger(__name__)

# Module-level singleton — shared across all handler calls.
_circuit_breaker = CircuitBreaker(
    window_seconds=60.0,
    threshold=5,
    cooldown_seconds=30.0,
)


def get_circuit_breaker() -> CircuitBreaker:
    """Return the module-level circuit breaker instance.

    Exposed for testing and health-check endpoints.
    """
    return _circuit_breaker


def _parse_enhanced_error(details: dict[str, object]) -> EnhancedError | None:
    """Attempt to parse an EnhancedError from the event details.

    Returns None if the payload does not contain a valid EnhancedError.
    The ``error`` key may hold a serialized EnhancedError dict, or the
    fields may be at the top level of ``details``.
    """
    # Try nested "error" dict first
    error_data = details.get("error")
    if isinstance(error_data, dict):
        try:
            return EnhancedError.model_validate(error_data)
        except (ValueError, TypeError):
            pass

    # Try top-level fields
    if "message" in details and "category" in details:
        try:
            return EnhancedError.model_validate(details)
        except (ValueError, TypeError):
            pass

    return None


def handle_service_error(event_details: dict[str, object]) -> None:
    """Process a SERVICE_ERROR event.

    Steps:
        1. Parse EnhancedError from details (best-effort)
        2. Route alert by severity (CRITICAL -> PagerDuty+Telegram, HIGH -> Telegram)
        3. Feed to circuit breaker
        4. If circuit opens -> emit CIRCUIT_OPEN event via log_event
    """
    source_service = str(event_details.get("service", "unknown"))
    venue = event_details.get("venue")
    venue_str = str(venue) if venue is not None else None

    enhanced_error = _parse_enhanced_error(event_details)

    # Build a summary for alerting
    if enhanced_error is not None:
        severity_str = enhanced_error.severity.value
        category_str = enhanced_error.category.value
        message = enhanced_error.message
        logger.info(
            "SERVICE_ERROR from %s (venue=%s): [%s/%s] %s",
            source_service,
            venue_str,
            severity_str,
            category_str,
            message,
        )
    else:
        severity_str = str(event_details.get("severity", "medium"))
        message = str(event_details.get("message", "Unknown error"))
        logger.info(
            "SERVICE_ERROR from %s (venue=%s): %s",
            source_service,
            venue_str,
            message,
        )

    # Record metric
    SERVICE_ERRORS_TOTAL.labels(service=source_service, venue=venue_str or "global").inc()

    # Route the alert through the standard routing pipeline.
    # CRITICAL/HIGH severity errors get PagerDuty; others get Telegram.
    alert_event_name = "SERVICE_ERROR_CRITICAL" if severity_str == "critical" else "SERVICE_ERROR"
    route_event(alert_event_name, event_details)

    # Feed to circuit breaker
    new_state = _circuit_breaker.record_error(source_service, venue_str)

    if new_state == "OPEN":
        # Emit CIRCUIT_OPEN lifecycle event for downstream consumers
        circuit_details: dict[str, str | int | float | bool | None] = {
            "service": source_service,
            "venue": venue_str,
            "error_count": _circuit_breaker.get_error_count(source_service, venue_str),
            "window_seconds": _circuit_breaker._window,
            "threshold": _circuit_breaker._threshold,
            "message": f"Circuit opened for {source_service}:{venue_str or 'global'} "
            f"({_circuit_breaker._threshold} errors in "
            f"{_circuit_breaker._window:.0f}s)",
        }
        log_event(LifecycleEventType.CIRCUIT_OPEN, details=circuit_details)
        CIRCUIT_STATE_TRANSITIONS.labels(
            service=source_service,
            venue=venue_str or "global",
            new_state="OPEN",
        ).inc()
        CIRCUITS_OPEN.inc()

        # Also route CIRCUIT_OPEN through the alert router for Telegram/PagerDuty
        route_event(
            "CIRCUIT_BREAKER_OPEN",
            {
                "message": circuit_details["message"],
                "service": source_service,
                "venue": venue_str,
            },
        )
        logger.warning(
            "CIRCUIT_OPEN emitted for %s:%s",
            source_service,
            venue_str or "global",
        )
