"""Tests for risk_threshold_rules."""

from decimal import Decimal

from unified_api_contracts import AlertCode

from alerting_service.rules.risk_threshold_rules import (
    evaluate_risk_thresholds,
    get_threshold_status,
)


class TestGetThresholdStatus:
    def test_ok(self) -> None:
        assert get_threshold_status(Decimal("3"), Decimal("7"), Decimal("10")) == "OK"

    def test_warning(self) -> None:
        assert get_threshold_status(Decimal("8"), Decimal("7"), Decimal("10")) == "WARNING"

    def test_critical(self) -> None:
        assert get_threshold_status(Decimal("11"), Decimal("7"), Decimal("10")) == "CRITICAL"

    def test_exact_warning(self) -> None:
        assert get_threshold_status(Decimal("7"), Decimal("7"), Decimal("10")) == "WARNING"

    def test_exact_critical(self) -> None:
        assert get_threshold_status(Decimal("10"), Decimal("7"), Decimal("10")) == "CRITICAL"


class TestEvaluateRiskThresholds:
    def test_all_ok(self) -> None:
        metrics = {"leverage": "3", "concentration": "0.2", "drawdown": "0.05"}
        alerts = evaluate_risk_thresholds(metrics)
        assert len(alerts) == 0

    def test_leverage_warning(self) -> None:
        metrics = {"leverage": "8.5", "concentration": "0.2", "drawdown": "0.05"}
        alerts = evaluate_risk_thresholds(metrics)
        assert len(alerts) == 1
        assert alerts[0]["metric_name"] == "leverage"
        assert alerts[0]["severity"] == "WARNING"

    def test_multiple_alerts(self) -> None:
        metrics = {"leverage": "8.5", "concentration": "0.42", "drawdown": "0.12"}
        alerts = evaluate_risk_thresholds(metrics)
        assert len(alerts) == 3

    def test_alert_has_required_fields(self) -> None:
        metrics = {"leverage": "11", "concentration": "0.2", "drawdown": "0.05"}
        alerts = evaluate_risk_thresholds(metrics)
        alert = alerts[0]
        assert "alert_id" in alert
        assert "metric_name" in alert
        assert "severity" in alert
        assert "message" in alert
        assert "created_at" in alert

    def test_client_id_propagated(self) -> None:
        metrics = {
            "client_id": "test-client",
            "leverage": "11",
            "concentration": "0",
            "drawdown": "0",
        }
        alerts = evaluate_risk_thresholds(metrics)
        assert alerts[0]["client_id"] == "test-client"

    def test_critical_breach_has_risk_rule_blocked_alert_code(self) -> None:
        """CRITICAL threshold breach must carry AlertCode.RISK_RULE_BLOCKED."""
        metrics = {"leverage": "11", "concentration": "0.2", "drawdown": "0.05"}
        alerts = evaluate_risk_thresholds(metrics)
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "CRITICAL"
        assert alerts[0]["alert_code"] == AlertCode.RISK_RULE_BLOCKED.value

    def test_warning_breach_has_risk_rule_monitor_fired_alert_code(self) -> None:
        """WARNING threshold breach must carry AlertCode.RISK_RULE_MONITOR_FIRED."""
        metrics = {"leverage": "8.5", "concentration": "0.2", "drawdown": "0.05"}
        alerts = evaluate_risk_thresholds(metrics)
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "WARNING"
        assert alerts[0]["alert_code"] == AlertCode.RISK_RULE_MONITOR_FIRED.value

    def test_alert_code_absent_when_ok(self) -> None:
        """No alerts emitted when all metrics are below warning threshold."""
        metrics = {"leverage": "3", "concentration": "0.2", "drawdown": "0.05"}
        alerts = evaluate_risk_thresholds(metrics)
        assert alerts == []
