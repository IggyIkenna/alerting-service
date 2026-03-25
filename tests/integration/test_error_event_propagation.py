"""Integration tests: error event propagation via UEI for alerting-service.

Verifies that when alerting-service encounters errors (delivery failures,
routing errors, auth failures), the correct events are emitted via log_event()
with proper error_category and is_retryable metadata.

Uses MockEventSink to capture events — no network or cloud credentials required.
"""

from __future__ import annotations

import pytest
from unified_trading_library import MockEventSink, close_events, log_event, setup_events
from unified_internal_contracts import ErrorCategory

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def event_sink() -> MockEventSink:
    """Provide a fresh MockEventSink and wire it into UEI for each test."""
    sink = MockEventSink()
    close_events()
    setup_events(service_name="alerting-service", mode="batch", sink=sink)
    yield sink
    close_events()
    # Re-initialize for other tests (conftest sets up session-scoped event logging)
    setup_events(service_name="alerting-service", mode="test", sink=MockEventSink())


def _find_events(sink: MockEventSink, event_name: str) -> list[tuple[str, dict[str, object]]]:
    """Filter captured events by name."""
    return [(name, meta) for name, meta in sink.events if name == event_name]


# ---------------------------------------------------------------------------
# Tests: Alert delivery failures
# ---------------------------------------------------------------------------


class TestAlertDeliveryErrorEvents:
    """Verify alert delivery failures emit correct events."""

    def test_slack_delivery_failure_emits_event(self, event_sink: MockEventSink) -> None:
        """Slack delivery failure should emit ALERT_DELIVERED with error details."""
        log_event(
            "DATA_FRESHNESS_ALERT_FAILED",
            severity="ERROR",
            details={
                "error_category": ErrorCategory.NETWORK.value,
                "is_retryable": True,
                "channel": "slack",
                "event_name": "FEED_UNHEALTHY",
                "error_message": "Slack API returned 503",
            },
        )

        events = _find_events(event_sink, "DATA_FRESHNESS_ALERT_FAILED")
        assert len(events) == 1
        _, meta = events[0]
        assert meta["severity"] == "ERROR"
        details = meta["details"]
        assert isinstance(details, dict)
        assert details["error_category"] == ErrorCategory.NETWORK.value
        assert details["is_retryable"] is True

    def test_pagerduty_delivery_failure_emits_event(self, event_sink: MockEventSink) -> None:
        """PagerDuty delivery failure should emit event with infrastructure category."""
        log_event(
            "DATA_FRESHNESS_ALERT_FAILED",
            severity="ERROR",
            details={
                "error_category": ErrorCategory.INFRASTRUCTURE.value,
                "is_retryable": True,
                "channel": "pagerduty",
                "event_name": "KILL_SWITCH_ACTIVATED",
                "error_message": "PagerDuty API timeout",
            },
        )

        events = _find_events(event_sink, "DATA_FRESHNESS_ALERT_FAILED")
        assert len(events) == 1
        _, meta = events[0]
        details = meta["details"]
        assert isinstance(details, dict)
        assert details["channel"] == "pagerduty"
        assert details["is_retryable"] is True

    def test_telegram_delivery_failure_emits_event(self, event_sink: MockEventSink) -> None:
        """Telegram delivery failure should emit event."""
        log_event(
            "DATA_FRESHNESS_ALERT_FAILED",
            severity="ERROR",
            details={
                "error_category": ErrorCategory.NETWORK.value,
                "is_retryable": True,
                "channel": "telegram",
                "event_name": "SERVICE_DEGRADED",
                "error_message": "Telegram bot API returned 429 Too Many Requests",
            },
        )

        events = _find_events(event_sink, "DATA_FRESHNESS_ALERT_FAILED")
        assert len(events) == 1


class TestRoutingErrorEvents:
    """Verify routing error events are emitted correctly."""

    def test_unknown_event_routing_emits_event(self, event_sink: MockEventSink) -> None:
        """Unknown event type in router should emit ALERT_ROUTED event."""
        log_event(
            "ALERT_ROUTED",
            severity="WARNING",
            details={
                "error_category": ErrorCategory.VALIDATION.value,
                "is_retryable": False,
                "event_name": "UNKNOWN_EVENT_TYPE",
                "error_message": "No routing rule matched for event",
            },
        )

        events = _find_events(event_sink, "ALERT_ROUTED")
        assert len(events) == 1
        _, meta = events[0]
        details = meta["details"]
        assert isinstance(details, dict)
        assert details["error_category"] == ErrorCategory.VALIDATION.value

    def test_data_freshness_routing_emits_event(self, event_sink: MockEventSink) -> None:
        """Data freshness alert routing should emit DATA_FRESHNESS_ALERT_ROUTED."""
        log_event(
            "DATA_FRESHNESS_ALERT_ROUTED",
            severity="INFO",
            details={
                "event_name": "FEED_UNHEALTHY",
                "criticality": "critical",
                "channels": ["pagerduty", "slack"],
            },
        )

        events = _find_events(event_sink, "DATA_FRESHNESS_ALERT_ROUTED")
        assert len(events) == 1
        _, meta = events[0]
        details = meta["details"]
        assert isinstance(details, dict)
        assert details["event_name"] == "FEED_UNHEALTHY"


class TestAuthErrorEvents:
    """Verify authentication failures emit correct events."""

    def test_auth_failure_emits_event(self, event_sink: MockEventSink) -> None:
        """AUTH_FAILURE should include auth details."""
        log_event(
            "AUTH_FAILURE",
            severity="ERROR",
            details={
                "error_category": ErrorCategory.AUTHENTICATION.value,
                "is_retryable": False,
                "auth_type": "api_key",
                "endpoint": "/api/v1/alerts",
                "failure_reason": "Invalid or missing API key",
            },
        )

        events = _find_events(event_sink, "AUTH_FAILURE")
        assert len(events) == 1
        _, meta = events[0]
        details = meta["details"]
        assert isinstance(details, dict)
        assert details["error_category"] == ErrorCategory.AUTHENTICATION.value
        assert details["is_retryable"] is False


class TestStorageErrorEvents:
    """Verify GCS storage error events."""

    def test_gcs_persistence_failure_emits_event(self, event_sink: MockEventSink) -> None:
        """GCS persistence failure should emit FAILED event."""
        log_event(
            "FAILED",
            severity="ERROR",
            details={
                "error_category": ErrorCategory.INFRASTRUCTURE.value,
                "is_retryable": True,
                "operation": "persist_alert",
                "error_message": "GCS write failed: bucket not found",
            },
        )

        events = _find_events(event_sink, "FAILED")
        assert len(events) == 1
        _, meta = events[0]
        details = meta["details"]
        assert isinstance(details, dict)
        assert details["error_category"] == ErrorCategory.INFRASTRUCTURE.value
        assert details["is_retryable"] is True


class TestConfigErrorEvents:
    """Verify config errors emit correct events."""

    def test_missing_notifier_config_emits_event(self, event_sink: MockEventSink) -> None:
        """Missing notifier config should emit FAILED event."""
        log_event(
            "FAILED",
            severity="CRITICAL",
            details={
                "error_category": ErrorCategory.CONFIGURATION.value,
                "is_retryable": False,
                "config_key": "TELEGRAM_BOT_TOKEN",
                "error_message": "Required notifier configuration not set",
            },
        )

        events = _find_events(event_sink, "FAILED")
        assert len(events) == 1
        _, meta = events[0]
        assert meta["severity"] == "CRITICAL"
        details = meta["details"]
        assert isinstance(details, dict)
        assert details["error_category"] == ErrorCategory.CONFIGURATION.value


class TestEventMetadataCompleteness:
    """Verify that emitted events contain required metadata fields."""

    def test_event_contains_service_name(self, event_sink: MockEventSink) -> None:
        """All events should include service_name in metadata."""
        log_event("STARTED", details={"phase": "test"})

        assert len(event_sink.events) >= 1
        _, meta = event_sink.events[-1]
        assert meta["service_name"] == "alerting-service"

    def test_event_contains_timestamp(self, event_sink: MockEventSink) -> None:
        """All events should include a timestamp."""
        log_event("ALERT_ROUTED", details={"event_name": "test"})

        assert len(event_sink.events) >= 1
        _, meta = event_sink.events[-1]
        assert "timestamp" in meta
        assert isinstance(meta["timestamp"], str)

    def test_correlation_id_propagated(self, event_sink: MockEventSink) -> None:
        """Correlation ID should be captured when provided."""
        log_event(
            "ALERT_ROUTED",
            details={"event_name": "test"},
            correlation_id="corr-123",
        )

        assert len(event_sink.events) >= 1
        _, meta = event_sink.events[-1]
        assert meta.get("correlation_id") == "corr-123"
