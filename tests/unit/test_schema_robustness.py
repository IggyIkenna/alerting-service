"""
Schema robustness tests for alerting-service.

Layer 1 smoke: verifies that core config and routing structures import cleanly
and that their schema invariants hold at module load time.
"""

from __future__ import annotations


class TestAlertingServiceConfig:
    """Smoke-test AlertingSystemConfig schema."""

    def test_config_importable(self) -> None:
        from alerting_service.config import AlertingSystemConfig

        assert AlertingSystemConfig is not None

    def test_config_has_service_name_field(self) -> None:
        from alerting_service.config import AlertingSystemConfig

        assert "service_name" in AlertingSystemConfig.model_fields

    def test_config_has_poll_interval_field(self) -> None:
        from alerting_service.config import AlertingSystemConfig

        assert "poll_interval_seconds" in AlertingSystemConfig.model_fields

    def test_poll_interval_default_is_positive(self) -> None:
        from alerting_service.config import AlertingSystemConfig

        field = AlertingSystemConfig.model_fields["poll_interval_seconds"]
        assert field.default is not None
        assert field.default > 0


class TestPagerDutyNotifier:
    """Smoke-test PagerDutySeverity type and routing constants."""

    def test_pagerduty_module_importable(self) -> None:
        from alerting_service.notifiers import pagerduty

        assert pagerduty is not None

    def test_pagerduty_severity_type_defined(self) -> None:
        from alerting_service.notifiers.pagerduty import PagerDutySeverity

        assert PagerDutySeverity is not None


class TestEventRouter:
    """Smoke-test routing constant sets."""

    def test_router_module_importable(self) -> None:
        from alerting_service.notifiers import router

        assert router is not None

    def test_pagerduty_events_dict_not_empty(self) -> None:
        from alerting_service.notifiers.router import _PAGERDUTY_EVENTS

        assert len(_PAGERDUTY_EVENTS) > 0

    def test_kill_switch_routes_to_pagerduty(self) -> None:
        from alerting_service.notifiers.router import _PAGERDUTY_EVENTS

        assert "KILL_SWITCH_ACTIVATED" in _PAGERDUTY_EVENTS
        assert _PAGERDUTY_EVENTS["KILL_SWITCH_ACTIVATED"] == "critical"

    def test_always_slack_events_is_frozenset(self) -> None:
        from alerting_service.notifiers.router import _ALWAYS_SLACK_EVENTS

        assert isinstance(_ALWAYS_SLACK_EVENTS, frozenset)
        assert "KILL_SWITCH_ACTIVATED" in _ALWAYS_SLACK_EVENTS
