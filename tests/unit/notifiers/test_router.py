"""Unit tests for the event router."""

from unittest.mock import MagicMock, patch

import pytest

from alerting_service.notifiers.router import route_event


@pytest.fixture
def mock_pd_send_event():
    """Patch PagerDuty send_event; returns True by default."""
    with patch("alerting_service.notifiers.router.pd_send_event", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_slack_send_message():
    """Patch Slack send_message; returns True by default."""
    with patch("alerting_service.notifiers.router.slack_send_message", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_log_event():
    """Suppress log_event calls."""
    with patch("alerting_service.notifiers.router.log_event") as mock:
        yield mock


class TestRouteEvent:
    def test_kill_switch_sends_to_pagerduty_and_slack(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_event("KILL_SWITCH_ACTIVATED", {"strategy": "s1"})

        mock_pd_send_event.assert_called_once()
        pd_kwargs = mock_pd_send_event.call_args.kwargs
        assert pd_kwargs["severity"] == "critical"
        assert "KILL_SWITCH_ACTIVATED" in pd_kwargs["summary"]

        mock_slack_send_message.assert_called_once()

    def test_circuit_breaker_sends_to_pagerduty_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_event("CIRCUIT_BREAKER_OPEN", {"venue": "binance"})

        mock_pd_send_event.assert_called_once()
        pd_kwargs = mock_pd_send_event.call_args.kwargs
        assert pd_kwargs["severity"] == "critical"
        assert "CIRCUIT_BREAKER_OPEN" in pd_kwargs["summary"]

        mock_slack_send_message.assert_not_called()

    def test_preflight_failed_sends_to_slack_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"session": "2026-01-01"})

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_called_once()
        slack_kwargs = mock_slack_send_message.call_args.kwargs
        assert "PREFLIGHT_FAILED" in slack_kwargs["text"]

    def test_service_degraded_sends_to_slack_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_event("SERVICE_DEGRADED", {"service": "market-data"})

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_called_once()

    def test_unknown_event_falls_back_to_slack(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_event("SOME_OTHER_EVENT", {})

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_called_once()

    def test_pagerduty_failure_is_logged_not_raised(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        mock_pd_send_event.return_value = False

        # Must not raise even when PagerDuty fails.
        route_event("CIRCUIT_BREAKER_OPEN", {})

    def test_slack_failure_is_logged_not_raised(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        mock_slack_send_message.return_value = False

        # Must not raise even when Slack fails.
        route_event("PREFLIGHT_FAILED", {})

    def test_details_forwarded_to_pagerduty(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        details: dict[str, object] = {"venue": "binance", "order_id": "ord-42"}
        route_event("CIRCUIT_BREAKER_OPEN", details)

        pd_kwargs = mock_pd_send_event.call_args.kwargs
        assert pd_kwargs["details"] == details
