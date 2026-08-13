"""Unit tests for ``dependency_health_event_handler``.

Verifies the wiring half of the dependency-health path: a payload with
``dependency_id`` + ``current_outage_seconds`` is evaluated by the rule ladder
and routed to the correct channels. Routing is asserted via a patched
``route_event_with_explicit_channels`` (no network), mirroring
``test_dr_event_handler``.
"""

from __future__ import annotations

from unittest.mock import patch

from unified_api_contracts.dependency import DependencyClass, DependencyHealthPolicy

from alerting_service.dependency_health_event_handler import (
    get_dependency_policy,
    handle_dependency_health_payload,
    handle_dependency_recovered_payload,
    set_dependency_policies,
)


def _policy(
    *,
    dependency_id: str = "binance_rest",
    fallback_available: bool = True,
    expected_recovery_time_seconds: int = 60,
    warning_buffer_seconds: int = 60,
    human_investigation_buffer_seconds: int = 900,
    hard_escalation_seconds: int = 1800,
) -> DependencyHealthPolicy:
    return DependencyHealthPolicy(
        dependency_id=dependency_id,
        dependency_class=DependencyClass.EXECUTION_CRITICAL_EXTERNAL,
        expected_recovery_time_seconds=expected_recovery_time_seconds,
        warning_buffer_seconds=warning_buffer_seconds,
        human_investigation_buffer_seconds=human_investigation_buffer_seconds,
        hard_escalation_seconds=hard_escalation_seconds,
        fallback_available=fallback_available,
        owner="platform",
        runbook_doc="codex/15-runbooks/incidents/rb_conn_001.md",
    )


class TestPolicyRegistry:
    def test_set_and_get_roundtrip(self) -> None:
        set_dependency_policies([_policy()])
        assert get_dependency_policy("binance_rest") is not None
        assert get_dependency_policy("unknown_dep") is None

    def test_set_replaces_wholesale(self) -> None:
        set_dependency_policies([_policy()])
        set_dependency_policies([_policy(dependency_id="bybit_rest")])
        assert get_dependency_policy("binance_rest") is None
        assert get_dependency_policy("bybit_rest") is not None


class TestHandleDependencyHealth:
    def test_critical_routes_to_pagerduty_and_telegram(self) -> None:
        set_dependency_policies([_policy()])
        with patch("alerting_service.dependency_health_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_dependency_health_payload({"dependency_id": "binance_rest", "current_outage_seconds": 1800.0})
        mock_route.assert_called_once()
        args, kwargs = mock_route.call_args
        assert kwargs["channels"] == {"pagerduty", "telegram"}
        assert kwargs["pd_severity"] == "critical"
        assert args[0] == "DEPENDENCY_DEGRADED_CRITICAL"

    def test_warning_routes_telegram_only(self) -> None:
        set_dependency_policies([_policy()])
        with patch("alerting_service.dependency_health_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_dependency_health_payload({"dependency_id": "binance_rest", "current_outage_seconds": 120.0})
        mock_route.assert_called_once()
        _args, kwargs = mock_route.call_args
        assert kwargs["channels"] == {"telegram"}
        assert kwargs["pd_severity"] is None

    def test_unregistered_dependency_is_skipped(self) -> None:
        set_dependency_policies([_policy()])
        with patch("alerting_service.dependency_health_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_dependency_health_payload({"dependency_id": "nope", "current_outage_seconds": 9999.0})
        mock_route.assert_not_called()

    def test_missing_dependency_id_is_skipped(self) -> None:
        set_dependency_policies([_policy()])
        with patch("alerting_service.dependency_health_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_dependency_health_payload({"current_outage_seconds": 9999.0})
        mock_route.assert_not_called()

    def test_no_alert_band_routes_nothing(self) -> None:
        set_dependency_policies([_policy()])
        with patch("alerting_service.dependency_health_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_dependency_health_payload({"dependency_id": "binance_rest", "current_outage_seconds": 30.0})
        mock_route.assert_not_called()


class TestHandleDependencyRecovered:
    def test_recovered_routes_info_to_telegram(self) -> None:
        set_dependency_policies([_policy()])
        with patch("alerting_service.dependency_health_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_dependency_recovered_payload({"dependency_id": "binance_rest", "previous_outage_seconds": 300.0})
        mock_route.assert_called_once()
        args, kwargs = mock_route.call_args
        assert args[0] == "DEPENDENCY_RECOVERED"
        assert kwargs["channels"] == {"telegram"}
        assert kwargs["pd_severity"] is None

    def test_zero_previous_outage_routes_nothing(self) -> None:
        set_dependency_policies([_policy()])
        with patch("alerting_service.dependency_health_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_dependency_recovered_payload({"dependency_id": "binance_rest", "previous_outage_seconds": 0})
        mock_route.assert_not_called()
