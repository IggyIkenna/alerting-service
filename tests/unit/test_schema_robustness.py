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

    def test_default_routing_rules_not_empty(self) -> None:
        from alerting_service.config import _default_routing_rules

        rules = _default_routing_rules()
        assert len(rules) > 0

    def test_kill_switch_rule_routes_to_pagerduty(self) -> None:
        # 2026-05-08 — UAC LIVE_ALERT_RULES split the legacy KILL_SWITCH_* wildcard
        # rule into 3 atomic per-code rules so each can carry its own
        # kill_switch_scope (GLOBAL / VENUE / ARCHETYPE). Test now uses one of the
        # real AlertCode values that maps to a kill-switch rule.
        from alerting_service.config import _default_routing_rules
        from alerting_service.notifiers.router import _match_routing_rules

        rules = _default_routing_rules()
        channels, severity = _match_routing_rules("KILL_SWITCH_DEFI_LIQUIDATION_RISK", rules)
        assert "pagerduty" in channels
        assert severity == "critical"

    def test_config_has_routing_rules_field(self) -> None:
        from alerting_service.config import AlertingSystemConfig

        assert "routing_rules" in AlertingSystemConfig.model_fields
