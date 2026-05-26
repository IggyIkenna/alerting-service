"""Handler for SERVICE_ERROR lifecycle events.

Processes SERVICE_ERROR events emitted by classify_and_emit_error() across
all services. Each error is:

1. Parsed into an EnhancedError (if the payload contains one)
2. Routed to the appropriate alert channel by severity
3. Fed to the circuit breaker for error-rate tracking
4. If the circuit opens -> emits a CIRCUIT_OPEN lifecycle event
5. If retries are exhausted -> creates a DeadLetterRecord and emits it

This module owns the singleton CircuitBreaker instance used by the
alerting-service.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from unified_api_contracts.internal import (  # noqa: qg-deep-import
    DeadLetterRecord,
    EnhancedError,
    ErrorCategory,
    ErrorRecoveryStrategy,
    LifecycleEventType,
)
from unified_trading_library import log_event

from .circuit_breaker import CircuitBreaker
from .metrics import (
    CIRCUIT_STATE_TRANSITIONS,
    CIRCUITS_OPEN,
    DEAD_LETTERS_TOTAL,
    SERVICE_ERRORS_TOTAL,
)
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

    # Check for dead letter condition: retries exhausted
    _check_dead_letter(event_details, source_service, venue_str, enhanced_error)

    # Feed to circuit breaker
    new_state = _circuit_breaker.record_error(source_service, venue_str)

    if new_state == "OPEN":
        # Emit CIRCUIT_OPEN lifecycle event for downstream consumers
        circuit_details: dict[str, object] = {
            "service": source_service,
            "venue": venue_str,
            "error_count": _circuit_breaker.get_error_count(source_service, venue_str),
            "window_seconds": _circuit_breaker.window,
            "threshold": _circuit_breaker.threshold,
            "message": f"Circuit opened for {source_service}:{venue_str or 'global'} "
            f"({_circuit_breaker.threshold} errors in "
            f"{_circuit_breaker.window:.0f}s)",
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


# Default max retries threshold. When an event's retry_count >= this value,
# the error is sent to the dead letter queue instead of being retried.
_MAX_RETRIES: int = 3


def _check_dead_letter(
    event_details: dict[str, object],
    source_service: str,
    venue_str: str | None,
    enhanced_error: EnhancedError | None,
) -> None:
    """Create a DeadLetterRecord if the event has exhausted retries.

    Checks for ``retry_count`` and ``max_retries`` in event_details.
    When retry_count >= max_retries, creates a DeadLetterRecord, emits
    it as a DEAD_LETTER lifecycle event, and increments the metric.
    """
    retry_count_raw = event_details.get("retry_count")
    if retry_count_raw is None:
        return

    retry_count = int(retry_count_raw) if isinstance(retry_count_raw, (int, float, str)) else 0
    max_retries_raw = event_details.get("max_retries", _MAX_RETRIES)
    max_retries = (
        int(max_retries_raw) if isinstance(max_retries_raw, (int, float, str)) else _MAX_RETRIES
    )

    if retry_count < max_retries:
        return

    # Retries exhausted -> dead letter
    now = datetime.now(UTC)
    first_failure_raw = event_details.get("first_failure_at")
    first_failure_at = (
        datetime.fromisoformat(str(first_failure_raw))
        if isinstance(first_failure_raw, str)
        else now
    )

    error_category = ErrorCategory.UNKNOWN
    error_message = str(event_details.get("message", "Unknown error"))
    if enhanced_error is not None:
        error_category = enhanced_error.category
        error_message = enhanced_error.message

    record = DeadLetterRecord(
        record_id=str(uuid.uuid4()),
        original_event=str(event_details.get("event_name", "SERVICE_ERROR")),
        original_payload=str(event_details),
        error_category=error_category,
        error_message=error_message,
        retry_count=retry_count,
        max_retries=max_retries,
        first_failure_at=first_failure_at,
        last_failure_at=now,
        source_service=source_service,
        dead_lettered_at=now,
        correlation_id=str(event_details.get("correlation_id"))
        if event_details.get("correlation_id")
        else None,
        trace_id=str(event_details.get("trace_id")) if event_details.get("trace_id") else None,
        venue=venue_str,
        recovery_strategy=ErrorRecoveryStrategy.DEAD_LETTER,
    )

    # Emit as lifecycle event for downstream persistence (GCS sink, dashboards)
    log_event(
        "DEAD_LETTER",
        details=record.model_dump(mode="json"),
    )

    DEAD_LETTERS_TOTAL.labels(service=source_service, venue=venue_str or "global").inc()

    logger.warning(
        "Dead-lettered error from %s (venue=%s): %s (retries=%d/%d)",
        source_service,
        venue_str or "global",
        error_message,
        retry_count,
        max_retries,
    )
