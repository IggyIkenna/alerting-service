"""Unit tests for data freshness alert routing rules."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from alerting_service.rules.data_freshness_rules import route_data_freshness_event

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pd_send_event() -> MagicMock:
    """Patch PagerDuty send_event; returns True by default."""
    with patch(
        "alerting_service.rules.data_freshness_rules.pd_send_event", return_value=True
    ) as mock:
        yield mock


@pytest.fixture
def mock_slack_send_message() -> MagicMock:
    """Patch Slack send_message; returns True by default."""
    with patch(
        "alerting_service.rules.data_freshness_rules.slack_send_message", return_value=True
    ) as mock:
        yield mock


@pytest.fixture
def mock_log_event() -> MagicMock:
    """Suppress log_event calls."""
    with patch("alerting_service.rules.data_freshness_rules.log_event") as mock:
        yield mock


# ---------------------------------------------------------------------------
# FEED_UNHEALTHY routing
# ---------------------------------------------------------------------------


class TestFeedUnhealthyRouting:
    def test_critical_sends_pagerduty_and_slack(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_data_freshness_event(
            "FEED_UNHEALTHY",
            {"criticality": "critical", "source": "binance", "detail": "feed down"},
        )

        mock_pd_send_event.assert_called_once()
        pd_kwargs = mock_pd_send_event.call_args.kwargs
        assert pd_kwargs["severity"] == "critical"
        assert "FEED_UNHEALTHY" in pd_kwargs["summary"]
        assert pd_kwargs["source"] == "binance"

        mock_slack_send_message.assert_called_once()

    def test_important_sends_slack_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_data_freshness_event(
            "FEED_UNHEALTHY",
            {"criticality": "important", "source": "glassnode"},
        )

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_called_once()

    def test_informational_logs_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_data_freshness_event(
            "FEED_UNHEALTHY",
            {"criticality": "informational", "source": "fred"},
        )

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_not_called()

    def test_missing_criticality_defaults_to_informational(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_data_freshness_event("FEED_UNHEALTHY", {"source": "unknown"})

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_not_called()

    def test_pagerduty_failure_is_logged_not_raised(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        mock_pd_send_event.return_value = False

        # Must not raise even when PagerDuty fails.
        route_data_freshness_event(
            "FEED_UNHEALTHY",
            {"criticality": "critical", "source": "binance"},
        )

        mock_log_event.assert_any_call(
            "DATA_FRESHNESS_ALERT_FAILED", details={"event_name": "FEED_UNHEALTHY"}
        )

    def test_slack_failure_emits_alert_failed(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        mock_slack_send_message.return_value = False

        route_data_freshness_event(
            "FEED_UNHEALTHY",
            {"criticality": "important", "source": "glassnode"},
        )

        mock_log_event.assert_any_call(
            "DATA_FRESHNESS_ALERT_FAILED", details={"event_name": "FEED_UNHEALTHY"}
        )


# ---------------------------------------------------------------------------
# DATA_STALE routing
# ---------------------------------------------------------------------------


class TestDataStaleRouting:
    def test_critical_sends_slack(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_data_freshness_event(
            "DATA_STALE",
            {"criticality": "critical", "source": "bybit", "age_seconds": 3},
        )

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_called_once()
        slack_kwargs = mock_slack_send_message.call_args.kwargs
        assert "DATA_STALE" in slack_kwargs["text"]

    def test_important_sends_slack(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_data_freshness_event(
            "DATA_STALE",
            {"criticality": "important", "source": "databento_intraday"},
        )

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_called_once()

    def test_informational_logs_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_data_freshness_event(
            "DATA_STALE",
            {"criticality": "informational", "source": "openbb"},
        )

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_not_called()

    def test_slack_failure_emits_alert_failed(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        mock_slack_send_message.return_value = False

        route_data_freshness_event(
            "DATA_STALE",
            {"criticality": "critical", "source": "binance"},
        )

        mock_log_event.assert_any_call(
            "DATA_FRESHNESS_ALERT_FAILED", details={"event_name": "DATA_STALE"}
        )


# ---------------------------------------------------------------------------
# DATA_GAP_DETECTED routing
# ---------------------------------------------------------------------------


class TestDataGapDetectedRouting:
    def test_significant_gap_sends_slack(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        """Gap > 2x cadence must alert via Slack."""
        route_data_freshness_event(
            "DATA_GAP_DETECTED",
            {
                "source": "binance",
                "age_seconds": 5,
                "expected_cadence_seconds": 1,
            },
        )

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_called_once()

    def test_minor_gap_suppressed(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        """Gap within 2x cadence must be suppressed (no notifier call)."""
        route_data_freshness_event(
            "DATA_GAP_DETECTED",
            {
                "source": "binance",
                "age_seconds": 1.5,
                "expected_cadence_seconds": 1,
            },
        )

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_not_called()

    def test_missing_age_falls_back_to_always_alert(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        """When age or cadence is absent, alert conservatively."""
        route_data_freshness_event(
            "DATA_GAP_DETECTED",
            {"source": "uniswap_v3"},
        )

        mock_slack_send_message.assert_called_once()

    def test_gap_exactly_2x_cadence_sends_slack(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        """Exactly 2x cadence is NOT > 2x so should be suppressed."""
        route_data_freshness_event(
            "DATA_GAP_DETECTED",
            {
                "source": "binance",
                "age_seconds": 2,
                "expected_cadence_seconds": 1,
            },
        )

        # age=2, cadence=1 → 2 > 2*1 is False → suppressed
        mock_slack_send_message.assert_not_called()

    def test_gap_failure_emits_alert_failed(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        mock_slack_send_message.return_value = False

        route_data_freshness_event(
            "DATA_GAP_DETECTED",
            {"source": "binance", "age_seconds": 10, "expected_cadence_seconds": 1},
        )

        mock_log_event.assert_any_call(
            "DATA_FRESHNESS_ALERT_FAILED", details={"event_name": "DATA_GAP_DETECTED"}
        )


# ---------------------------------------------------------------------------
# Unknown event fallback
# ---------------------------------------------------------------------------


class TestUnknownFreshnessEvent:
    def test_unknown_event_falls_back_to_slack(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_data_freshness_event("DATA_AVAILABILITY_RESTORED", {"source": "binance"})

        mock_pd_send_event.assert_not_called()
        mock_slack_send_message.assert_called_once()

    def test_routed_event_emits_routed_log(
        self,
        mock_pd_send_event: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        route_data_freshness_event(
            "FEED_UNHEALTHY",
            {"criticality": "critical", "source": "okx"},
        )

        mock_log_event.assert_any_call(
            "DATA_FRESHNESS_ALERT_ROUTED", details={"event_name": "FEED_UNHEALTHY"}
        )
