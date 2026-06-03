"""Integration test for pvl-p22a: mode-aware risk threshold alerts.

Verifies that alerting-service rules consume ``mode: OperationalMode`` from
the instruction envelope and apply per-mode threshold logic:

  LIVE      -- default thresholds; alerts have wake_operator=False
  PAPER     -- 1.5x looser thresholds (same value may not fire)
  MANUAL    -- same thresholds as LIVE; alerts have wake_operator=True
  BACKTEST  -- no alerts regardless of metric values
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from unified_api_contracts.internal.modes import OperationalMode

from alerting_service.rules.risk_threshold_rules import evaluate_risk_thresholds


class TestModeAwareRiskThresholds:
    """Integration tests for mode-tagged alert decisions (pvl-p22a)."""

    # leverage=8.5 is above LIVE warning (7) but below PAPER warning (7 x 1.5 = 10.5)
    _BORDERLINE_METRICS: ClassVar[dict[str, object]] = {
        "leverage": "8.5",
        "concentration": "0.2",
        "drawdown": "0.05",
        "client_id": "integration-test-client",
    }

    def test_live_mode_fires_at_default_thresholds(self) -> None:
        alerts = evaluate_risk_thresholds(self._BORDERLINE_METRICS, mode=OperationalMode.LIVE)
        assert len(alerts) == 1
        assert alerts[0]["metric_name"] == "leverage"
        assert alerts[0]["severity"] == "WARNING"

    def test_paper_mode_looser_threshold_no_alert(self) -> None:
        """PAPER threshold is 1.5x live -- leverage 8.5 is below 10.5."""
        alerts = evaluate_risk_thresholds(self._BORDERLINE_METRICS, mode=OperationalMode.PAPER)
        assert len(alerts) == 0

    def test_paper_mode_fires_above_scaled_threshold(self) -> None:
        """leverage=11 exceeds PAPER warning (10.5) -- alert fires."""
        metrics: dict[str, object] = {
            "leverage": "11",
            "concentration": "0.2",
            "drawdown": "0.05",
        }
        alerts = evaluate_risk_thresholds(metrics, mode=OperationalMode.PAPER)
        assert len(alerts) == 1
        assert alerts[0]["mode"] == "paper"
        assert alerts[0]["wake_operator"] is False

    def test_manual_mode_same_thresholds_as_live(self) -> None:
        """MANUAL uses live thresholds -- leverage 8.5 fires."""
        alerts = evaluate_risk_thresholds(self._BORDERLINE_METRICS, mode=OperationalMode.MANUAL)
        assert len(alerts) == 1
        assert alerts[0]["metric_name"] == "leverage"

    def test_manual_mode_sets_wake_operator_flag(self) -> None:
        """MANUAL alerts wake the operator instead of paging on-call."""
        alerts = evaluate_risk_thresholds(self._BORDERLINE_METRICS, mode=OperationalMode.MANUAL)
        assert alerts[0]["wake_operator"] is True

    def test_live_mode_does_not_set_wake_operator(self) -> None:
        alerts = evaluate_risk_thresholds(self._BORDERLINE_METRICS, mode=OperationalMode.LIVE)
        assert alerts[0]["wake_operator"] is False

    def test_backtest_mode_suppresses_all_alerts(self) -> None:
        """Extreme backtest metrics must not generate alerts."""
        extreme: dict[str, object] = {
            "leverage": "999",
            "concentration": "0.99",
            "drawdown": "0.99",
        }
        alerts = evaluate_risk_thresholds(extreme, mode=OperationalMode.BACKTEST)
        assert alerts == []

    def test_alerts_include_mode_field(self) -> None:
        """Alert dict carries mode for downstream observability."""
        for mode in (OperationalMode.LIVE, OperationalMode.PAPER, OperationalMode.MANUAL):
            metrics: dict[str, object] = {
                "leverage": "11",
                "concentration": "0.6",
                "drawdown": "0.2",
            }
            alerts = evaluate_risk_thresholds(metrics, mode=mode)
            for alert in alerts:
                assert "mode" in alert
                assert alert["mode"] == mode.value

    def test_default_mode_is_live(self) -> None:
        """Calling without mode= defaults to LIVE behaviour."""
        alerts_default = evaluate_risk_thresholds(self._BORDERLINE_METRICS)
        alerts_live = evaluate_risk_thresholds(self._BORDERLINE_METRICS, mode=OperationalMode.LIVE)
        assert len(alerts_default) == len(alerts_live)


class TestModeAwareRiskThresholdsMultipleAlerts:
    """Multi-metric firing under mode-aware thresholds."""

    def test_paper_mode_all_below_scaled_threshold(self) -> None:
        """Values that fire in LIVE but not in PAPER (all below 1.5x base)."""
        metrics: dict[str, object] = {
            "leverage": "8.5",  # LIVE fires (>7), PAPER no (10.5)
            "concentration": "0.4",  # LIVE fires (>0.35), PAPER no (0.525)
            "drawdown": "0.12",  # LIVE fires (>0.10), PAPER no (0.15)
        }
        live_alerts = evaluate_risk_thresholds(metrics, mode=OperationalMode.LIVE)
        paper_alerts = evaluate_risk_thresholds(metrics, mode=OperationalMode.PAPER)
        assert len(live_alerts) == 3
        assert len(paper_alerts) == 0

    @pytest.mark.parametrize("mode", [OperationalMode.LIVE, OperationalMode.MANUAL, OperationalMode.PAPER])
    def test_all_non_backtest_modes_have_mode_field(self, mode: OperationalMode) -> None:
        metrics: dict[str, object] = {"leverage": "11", "concentration": "0.6", "drawdown": "0.2"}
        alerts = evaluate_risk_thresholds(metrics, mode=mode)
        assert all(a["mode"] == mode.value for a in alerts)
