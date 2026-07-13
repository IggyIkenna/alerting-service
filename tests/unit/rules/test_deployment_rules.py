"""Unit tests for deployment lifecycle alert routing rules."""

import pytest
from unified_api_contracts import AlertSeverity
from unified_trading_library import (
    DEPLOYMENT_COMPLETED,
    DEPLOYMENT_DIGEST,
    DEPLOYMENT_FAILED,
    DEPLOYMENT_STARTED,
)

from alerting_service.rules.deployment_rules import (
    deployment_rule_for,
    is_deployment_event,
)

pytestmark = pytest.mark.unit


class TestDeploymentRuleFor:
    def test_failed_is_critical(self) -> None:
        rule = deployment_rule_for(DEPLOYMENT_FAILED)
        assert rule is not None
        assert rule.severity is AlertSeverity.CRITICAL

    def test_started_completed_are_info(self) -> None:
        for event in (DEPLOYMENT_STARTED, DEPLOYMENT_COMPLETED):
            rule = deployment_rule_for(event)
            assert rule is not None
            assert rule.severity is AlertSeverity.INFO

    def test_digest_is_info_channel_only(self) -> None:
        # Parity #5: the daily deployment-estate digest is an INFO channel mirror,
        # never a page.
        rule = deployment_rule_for(DEPLOYMENT_DIGEST)
        assert rule is not None
        assert rule.severity is AlertSeverity.INFO
        assert is_deployment_event(DEPLOYMENT_DIGEST)

    def test_non_deployment_event_returns_none(self) -> None:
        assert deployment_rule_for("DP_VM_STALL") is None
        assert deployment_rule_for("KILL_SWITCH_ACTIVATED") is None

    def test_is_deployment_event(self) -> None:
        assert is_deployment_event(DEPLOYMENT_FAILED)
        assert not is_deployment_event("SOME_OTHER_EVENT")
