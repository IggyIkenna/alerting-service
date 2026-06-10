"""Unit tests for evaluate_dependency_health + evaluate_dependency_recovered.

Pure-function tests — no mocks, credential-free.

Plan: connectivity_dependency_buffer_policy_2026_05_23.md Phase 3 P0.6-P0.7.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from unified_api_contracts.alerting import AlertSeverity
from unified_api_contracts.dependency import DependencyClass, DependencyHealthPolicy

from alerting_service.rules.connectivity_rules import (
    evaluate_dependency_health,
    evaluate_dependency_recovered,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _policy(
    *,
    dependency_id: str = "test_dep",
    dependency_class: DependencyClass = DependencyClass.EXECUTION_CRITICAL_EXTERNAL,
    expected_recovery_time_seconds: int = 60,
    warning_buffer_seconds: int = 60,
    human_investigation_buffer_seconds: int = 900,
    hard_escalation_seconds: int = 1800,
    fallback_available: bool = True,
    protected_mode_available: bool = False,
    owner: str = "platform",
    runbook_doc: str = "codex/15-runbooks/incidents/rb_conn_001.md",
) -> DependencyHealthPolicy:
    return DependencyHealthPolicy(
        dependency_id=dependency_id,
        dependency_class=dependency_class,
        expected_recovery_time_seconds=expected_recovery_time_seconds,
        warning_buffer_seconds=warning_buffer_seconds,
        human_investigation_buffer_seconds=human_investigation_buffer_seconds,
        hard_escalation_seconds=hard_escalation_seconds,
        fallback_available=fallback_available,
        protected_mode_available=protected_mode_available,
        owner=owner,
        runbook_doc=runbook_doc,
    )


def _event(outage_seconds: float, dependency_id: str = "test_dep") -> dict[str, object]:
    return {
        "dependency_id": dependency_id,
        "current_outage_seconds": outage_seconds,
        "venue": "binance",
    }


# ── No-alert band ─────────────────────────────────────────────────────────────


class TestNoAlert:
    def test_zero_outage_returns_empty(self) -> None:
        assert evaluate_dependency_health(_event(0), _policy()) == []

    def test_negative_outage_returns_empty(self) -> None:
        assert evaluate_dependency_health(_event(-10), _policy()) == []

    def test_within_expected_recovery_returns_empty(self) -> None:
        # outage < expected_recovery_time_seconds (60s) → no alert
        assert evaluate_dependency_health(_event(30), _policy()) == []

    def test_between_expected_and_warn_returns_empty(self) -> None:
        # outage in [60, 120) — above expected, below warn_at
        assert evaluate_dependency_health(_event(90), _policy()) == []


# ── WARNING band ──────────────────────────────────────────────────────────────


class TestWarningBand:
    def test_at_warn_at_threshold_returns_warning(self) -> None:
        # warn_at = expected(60) + warning_buffer(60) = 120
        alerts = evaluate_dependency_health(_event(120), _policy())
        assert len(alerts) == 1
        a = alerts[0]
        assert a["severity"] == AlertSeverity.WARN
        assert a["sev1_escalate"] is False
        assert a["delivery_channel"] == "telegram"
        assert a["rule_id"] == "DEPENDENCY_DEGRADED_WARN"

    def test_just_below_sev1_is_warning(self) -> None:
        # sev1_at = 120 + 900 = 1020; at 1019 → WARN (no sev1)
        alerts = evaluate_dependency_health(_event(1019), _policy())
        assert len(alerts) == 1
        assert alerts[0]["severity"] == AlertSeverity.WARN
        assert alerts[0]["sev1_escalate"] is False


# ── SEV1 band ─────────────────────────────────────────────────────────────────


class TestSev1Band:
    def test_at_sev1_threshold_escalates(self) -> None:
        # sev1_at = 60 + 60 + 900 = 1020
        alerts = evaluate_dependency_health(_event(1020), _policy())
        assert len(alerts) == 1
        a = alerts[0]
        assert a["severity"] == AlertSeverity.WARN
        assert a["sev1_escalate"] is True
        assert a["rule_id"] == "DEPENDENCY_DEGRADED_SEV1"

    def test_just_below_hard_escalation_is_sev1(self) -> None:
        alerts = evaluate_dependency_health(_event(1799), _policy())
        assert len(alerts) == 1
        assert alerts[0]["sev1_escalate"] is True
        assert alerts[0]["severity"] == AlertSeverity.WARN


# ── CRITICAL / SEV0 band ─────────────────────────────────────────────────────


class TestCriticalBand:
    def test_at_hard_escalation_returns_critical(self) -> None:
        alerts = evaluate_dependency_health(_event(1800), _policy())
        assert len(alerts) == 1
        a = alerts[0]
        assert a["severity"] == "CRITICAL"
        assert a["delivery_channel"] == "pagerduty+telegram"
        assert a["rule_id"] == "DEPENDENCY_DEGRADED_CRITICAL"
        assert a["sev1_escalate"] is False

    def test_no_fallback_any_outage_is_critical(self) -> None:
        p = _policy(fallback_available=False)
        # Any outage > 0 with no fallback → immediate SEV0
        alerts = evaluate_dependency_health(_event(10), p)
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "CRITICAL"

    def test_no_fallback_zero_outage_still_empty(self) -> None:
        p = _policy(fallback_available=False)
        assert evaluate_dependency_health(_event(0), p) == []


# ── Alert dict fields ─────────────────────────────────────────────────────────


class TestAlertFields:
    def test_dependency_id_propagated(self) -> None:
        p = _policy(dependency_id="binance_rest")
        alerts = evaluate_dependency_health(_event(500, "binance_rest"), p)
        assert alerts[0]["dependency_id"] == "binance_rest"

    def test_dependency_class_propagated(self) -> None:
        p = _policy(dependency_class=DependencyClass.ALERTING_AND_OBSERVABILITY)
        alerts = evaluate_dependency_health(_event(500), p)
        assert alerts[0]["dependency_class"] == "ALERTING_AND_OBSERVABILITY"

    def test_runbook_doc_propagated(self) -> None:
        p = _policy(runbook_doc="codex/15-runbooks/incidents/rb_conn_002.md")
        alerts = evaluate_dependency_health(_event(500), p)
        assert alerts[0]["runbook_doc"] == "codex/15-runbooks/incidents/rb_conn_002.md"

    def test_created_at_is_parseable_iso(self) -> None:
        alerts = evaluate_dependency_health(_event(500), _policy())
        assert datetime.fromisoformat(str(alerts[0]["created_at"]))

    def test_delivered_is_false(self) -> None:
        alerts = evaluate_dependency_health(_event(500), _policy())
        assert alerts[0]["delivered"] is False


# ── Recovered ─────────────────────────────────────────────────────────────────


class TestDependencyRecovered:
    def test_previous_outage_emits_recovered_alert(self) -> None:
        event = {"dependency_id": "binance_rest", "previous_outage_seconds": 300.0}
        alerts = evaluate_dependency_recovered(event, _policy())
        assert len(alerts) == 1
        a = alerts[0]
        assert a["rule_id"] == "DEPENDENCY_RECOVERED"
        assert a["severity"] == "INFO"
        assert a["delivery_channel"] == "telegram"

    def test_zero_previous_outage_returns_empty(self) -> None:
        event = {"dependency_id": "x", "previous_outage_seconds": 0}
        assert evaluate_dependency_recovered(event, _policy()) == []

    def test_message_includes_outage_duration(self) -> None:
        event = {"dependency_id": "helius", "previous_outage_seconds": 180.0}
        alerts = evaluate_dependency_recovered(event, _policy())
        assert "180s" in str(alerts[0]["message"])


# ── Edge cases ────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_missing_outage_key_returns_empty(self) -> None:
        assert evaluate_dependency_health({"dependency_id": "x"}, _policy()) == []

    def test_non_numeric_outage_returns_empty(self) -> None:
        assert evaluate_dependency_health({"current_outage_seconds": "bad"}, _policy()) == []

    @pytest.mark.parametrize("dep_class", list(DependencyClass))
    def test_all_dependency_classes_accepted(self, dep_class: DependencyClass) -> None:
        p = _policy(dependency_class=dep_class)
        alerts = evaluate_dependency_health(_event(500), p)
        assert len(alerts) == 1
        assert alerts[0]["dependency_class"] == dep_class.value
