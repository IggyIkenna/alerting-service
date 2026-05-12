"""Unit tests for ``risk_rule_event_handler`` — Phase 5.B of
``risk_simulations_limits_alerting_2026_05_10``.

Coverage:
- Each ``RiskRuleConsequence``'s ``RiskRuleFiredEvent`` routes to the correct
  channel + severity tier (BLOCK→HIGH+PagerDuty, SCALE_DOWN→WARN+Telegram,
  MONITOR/TEST_ONLY→INFO+LogOnly).
- ``CONSEQUENCE_ALERT_CODES`` coverage — every consequence maps to a closed-set
  AlertCode that has a seeded ``LIVE_ALERT_RULES`` routing entry.
- Payload deserialization (malformed → drop, valid → route).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from unified_api_contracts import LIVE_ALERT_RULES, AlertChannel, AlertSeverity
from unified_api_contracts.alerting import AlertCode
from unified_api_contracts.risk import (
    ALL_RULES,
    CONSEQUENCE_ALERT_CODES,
    RiskRule,
    RiskRuleConsequence,
    RiskRuleFiredEvent,
    risk_rule_fired_event,
)

from alerting_service.config import _default_routing_rules
from alerting_service.notifiers.router import _match_routing_rules
from alerting_service.risk_rule_event_handler import (
    _details_payload,
    handle_risk_rule_fired_event,
    handle_risk_rule_fired_payload,
)


def _rule_for_consequence(consequence: RiskRuleConsequence) -> RiskRule:
    for rule in ALL_RULES:
        if rule.consequence is consequence:
            return rule
    raise AssertionError(f"no registry rule with consequence={consequence}")


def _event_for_consequence(consequence: RiskRuleConsequence) -> RiskRuleFiredEvent:
    rule = _rule_for_consequence(consequence)
    return risk_rule_fired_event(
        rule,
        fired_at=datetime.now(UTC),
        instruction_id="instr-123",
        trigger_detail={"observed": "1.5", "limit": "1.0"},
        metadata={"venue": "binance"},
    )


# ---------------------------------------------------------------------------
# CONSEQUENCE_ALERT_CODES coverage
# ---------------------------------------------------------------------------
class TestConsequenceAlertCodeCoverage:
    def test_every_consequence_has_alert_code(self) -> None:
        for consequence in RiskRuleConsequence:
            assert consequence in CONSEQUENCE_ALERT_CODES

    def test_every_consequence_alert_code_has_routing_rule(self) -> None:
        seeded = {rule.code.value for rule in LIVE_ALERT_RULES}
        for consequence, code_name in CONSEQUENCE_ALERT_CODES.items():
            assert code_name in seeded, f"{consequence} → {code_name} has no LIVE_ALERT_RULES entry"
            # The code name must be a valid closed-set AlertCode.
            assert AlertCode(code_name) is not None


# ---------------------------------------------------------------------------
# Per-consequence channel + severity routing
# ---------------------------------------------------------------------------
_EXPECTED_TIER: dict[RiskRuleConsequence, tuple[AlertSeverity, set[str]]] = {
    RiskRuleConsequence.BLOCK: (AlertSeverity.HIGH, {"pagerduty", "telegram"}),
    RiskRuleConsequence.SCALE_DOWN: (AlertSeverity.WARN, {"telegram"}),
    RiskRuleConsequence.MONITOR: (AlertSeverity.INFO, {"log_only"}),
    RiskRuleConsequence.TEST_ONLY: (AlertSeverity.INFO, {"log_only"}),
}


class TestPerConsequenceRouting:
    @pytest.mark.parametrize("consequence", list(RiskRuleConsequence))
    def test_event_routes_with_alert_code_name(self, consequence: RiskRuleConsequence) -> None:
        event = _event_for_consequence(consequence)
        with patch("alerting_service.risk_rule_event_handler.route_event") as mock_route:
            handle_risk_rule_fired_event(event)
        mock_route.assert_called_once()
        event_name, details = mock_route.call_args[0]
        assert event_name == CONSEQUENCE_ALERT_CODES[consequence]
        assert details["consequence"] == consequence.value
        assert details["severity"] == event.alerting_severity.value
        # trigger_detail rendered into the body
        assert details["trigger_observed"] == "1.5"
        assert details["trigger_limit"] == "1.0"
        assert details["meta_venue"] == "binance"

    @pytest.mark.parametrize("consequence", list(RiskRuleConsequence))
    def test_routing_rules_resolve_expected_tier(self, consequence: RiskRuleConsequence) -> None:
        """The router's config-driven channel resolution for each RISK_RULE code
        matches the § 7 seam-diagram tier."""
        expected_sev, expected_channels = _EXPECTED_TIER[consequence]
        code_name = CONSEQUENCE_ALERT_CODES[consequence]
        channels, _pd_sev = _match_routing_rules(code_name, _default_routing_rules())
        assert channels == expected_channels, f"{consequence}: {channels} != {expected_channels}"
        # The seeded rule severity matches the seam diagram.
        rule = next(r for r in LIVE_ALERT_RULES if r.code.value == code_name)
        assert rule.severity is expected_sev

    def test_block_event_pages(self) -> None:
        event = _event_for_consequence(RiskRuleConsequence.BLOCK)
        code_name = event.alert_code.value
        channels, _ = _match_routing_rules(code_name, _default_routing_rules())
        assert "pagerduty" in channels and "telegram" in channels

    def test_monitor_event_is_log_only(self) -> None:
        event = _event_for_consequence(RiskRuleConsequence.MONITOR)
        channels, _ = _match_routing_rules(event.alert_code.value, _default_routing_rules())
        assert channels == {"log_only"}
        assert AlertChannel.LOG_ONLY.value == "log_only"


# ---------------------------------------------------------------------------
# Payload deserialization
# ---------------------------------------------------------------------------
class TestPayloadDeserialization:
    def test_valid_payload_routes(self) -> None:
        event = _event_for_consequence(RiskRuleConsequence.BLOCK)
        payload = event.model_dump(mode="json")
        with patch("alerting_service.risk_rule_event_handler.route_event") as mock_route:
            handle_risk_rule_fired_payload(payload)
        mock_route.assert_called_once()
        assert mock_route.call_args[0][0] == event.alert_code.value

    def test_malformed_payload_dropped(self) -> None:
        with patch("alerting_service.risk_rule_event_handler.route_event") as mock_route:
            handle_risk_rule_fired_payload({"not": "a risk rule event"})
        mock_route.assert_not_called()

    def test_details_payload_includes_kill_switch_fields_when_set(self) -> None:
        # Find a registry rule that triggers a kill switch, if any.
        ks_rule: RiskRule | None = None
        for rule in ALL_RULES:
            event = risk_rule_fired_event(rule, fired_at=datetime.now(UTC))
            if event.triggers_kill_switch and event.kill_switch_scope is not None:
                ks_rule = rule
                break
        if ks_rule is None:
            pytest.skip("no kill-switch-triggering rule in registry")
        event = risk_rule_fired_event(ks_rule, fired_at=datetime.now(UTC))
        payload = _details_payload(event)
        assert payload["triggers_kill_switch"] is True
        assert payload["kill_switch_scope"] == event.kill_switch_scope.value
