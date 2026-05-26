"""Unit tests for the error_event_handler module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from alerting_service.circuit_breaker import STATE_CLOSED, STATE_OPEN
from alerting_service.error_event_handler import (
    _parse_enhanced_error,
    get_circuit_breaker,
    handle_service_error,
)


class TestParseEnhancedError:
    def test_parses_from_nested_error_dict(self) -> None:
        details: dict[str, object] = {
            "error": {
                "message": "Connection timeout",
                "category": "timeout",
                "severity": "high",
                "recovery_strategy": "retry_with_backoff",
                "context": {"service": "market-tick-data-service", "venue": "binance"},
            },
        }
        err = _parse_enhanced_error(details)
        assert err is not None
        assert err.message == "Connection timeout"
        assert err.category.value == "timeout"

    def test_parses_from_top_level_fields(self) -> None:
        details: dict[str, object] = {
            "message": "Rate limit exceeded",
            "category": "rate_limit",
            "severity": "medium",
            "recovery_strategy": "retry_with_backoff",
            "context": {"service": "execution-service"},
        }
        err = _parse_enhanced_error(details)
        assert err is not None
        assert err.message == "Rate limit exceeded"

    def test_returns_none_for_unparseable(self) -> None:
        details: dict[str, object] = {"some_key": "some_value"}
        assert _parse_enhanced_error(details) is None

    def test_returns_none_for_invalid_error_dict(self) -> None:
        details: dict[str, object] = {"error": {"not": "an enhanced error"}}
        assert _parse_enhanced_error(details) is None


class TestHandleServiceError:
    def _make_error_details(
        self,
        service: str = "execution-service",
        venue: str = "binance",
        severity: str = "high",
    ) -> dict[str, object]:
        return {
            "service": service,
            "venue": venue,
            "error": {
                "message": "Test error",
                "category": "timeout",
                "severity": severity,
                "recovery_strategy": "retry",
                "context": {"service": service, "venue": venue},
            },
        }

    @patch("alerting_service.error_event_handler.route_event")
    @patch("alerting_service.error_event_handler.log_event")
    def test_routes_non_critical_to_service_error(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        # Reset circuit breaker state
        cb = get_circuit_breaker()
        cb._errors.clear()
        cb._states.clear()
        cb._open_since.clear()

        details = self._make_error_details(severity="high")
        handle_service_error(details)

        mock_route_event.assert_called_once()
        call_args = mock_route_event.call_args
        assert call_args.args[0] == "SERVICE_ERROR"

    @patch("alerting_service.error_event_handler.route_event")
    @patch("alerting_service.error_event_handler.log_event")
    def test_routes_critical_to_service_error_critical(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        cb = get_circuit_breaker()
        cb._errors.clear()
        cb._states.clear()
        cb._open_since.clear()

        details = self._make_error_details(severity="critical")
        handle_service_error(details)

        mock_route_event.assert_called_once()
        call_args = mock_route_event.call_args
        assert call_args.args[0] == "SERVICE_ERROR_CRITICAL"

    @patch("alerting_service.error_event_handler.route_event")
    @patch("alerting_service.error_event_handler.log_event")
    def test_circuit_opens_after_threshold(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        cb = get_circuit_breaker()
        cb._errors.clear()
        cb._states.clear()
        cb._open_since.clear()

        # Default threshold is 5
        for _ in range(5):
            handle_service_error(self._make_error_details())

        assert cb.get_state("execution-service", "binance") == STATE_OPEN

        # Verify CIRCUIT_OPEN log_event was emitted
        circuit_open_calls = [
            call for call in mock_log_event.call_args_list if str(call.args[0]) == "CIRCUIT_OPEN"
        ]
        assert len(circuit_open_calls) == 1

        # Verify CIRCUIT_BREAKER_OPEN was routed for alerting
        circuit_route_calls = [
            call
            for call in mock_route_event.call_args_list
            if call.args[0] == "CIRCUIT_BREAKER_OPEN"
        ]
        assert len(circuit_route_calls) == 1

    @patch("alerting_service.error_event_handler.route_event")
    @patch("alerting_service.error_event_handler.log_event")
    def test_below_threshold_does_not_open_circuit(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        cb = get_circuit_breaker()
        cb._errors.clear()
        cb._states.clear()
        cb._open_since.clear()

        for _ in range(4):
            handle_service_error(self._make_error_details())

        assert cb.get_state("execution-service", "binance") == STATE_CLOSED

        circuit_open_calls = [
            call for call in mock_log_event.call_args_list if str(call.args[0]) == "CIRCUIT_OPEN"
        ]
        assert len(circuit_open_calls) == 0

    @patch("alerting_service.error_event_handler.route_event")
    @patch("alerting_service.error_event_handler.log_event")
    def test_handles_details_without_enhanced_error(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        cb = get_circuit_breaker()
        cb._errors.clear()
        cb._states.clear()
        cb._open_since.clear()

        details: dict[str, object] = {
            "service": "some-service",
            "message": "Something went wrong",
            "severity": "medium",
        }
        handle_service_error(details)

        mock_route_event.assert_called_once()
        call_args = mock_route_event.call_args
        assert call_args.args[0] == "SERVICE_ERROR"

    @patch("alerting_service.error_event_handler.route_event")
    @patch("alerting_service.error_event_handler.log_event")
    def test_different_venues_have_separate_circuits(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        cb = get_circuit_breaker()
        cb._errors.clear()
        cb._states.clear()
        cb._open_since.clear()

        # 5 errors on binance -> opens
        for _ in range(5):
            handle_service_error(self._make_error_details(venue="binance"))
        assert cb.get_state("execution-service", "binance") == STATE_OPEN

        # coinbase should still be closed
        assert cb.get_state("execution-service", "coinbase") == STATE_CLOSED
