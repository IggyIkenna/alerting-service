"""Unit tests for alerting_service.rules.reconciliation_rules.

Pure-function tests — no mocks, no network, credential-free.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from alerting_service.rules.reconciliation_rules import (
    _severity_from_pct,
    evaluate_balance_discrepancy,
    evaluate_pnl_discrepancy,
    evaluate_position_discrepancy,
)

# ---------------------------------------------------------------------------
# _severity_from_pct
# ---------------------------------------------------------------------------


class TestSeverityFromPct:
    def test_ok_below_warning_threshold(self) -> None:
        assert _severity_from_pct("0.005") == "OK"

    def test_ok_zero(self) -> None:
        assert _severity_from_pct("0") == "OK"

    def test_warning_at_threshold(self) -> None:
        assert _severity_from_pct("0.01") == "WARNING"

    def test_warning_above_threshold_below_critical(self) -> None:
        assert _severity_from_pct("0.03") == "WARNING"

    def test_critical_at_threshold(self) -> None:
        assert _severity_from_pct("0.05") == "CRITICAL"

    def test_critical_above_threshold(self) -> None:
        assert _severity_from_pct("0.20") == "CRITICAL"

    def test_percent_format_5_percent(self) -> None:
        # "5.00%" → strip "%" → "5.00" > 1 → divide by 100 → 0.05 → CRITICAL
        assert _severity_from_pct("5.00%") == "CRITICAL"

    def test_percent_format_2_percent_warning(self) -> None:
        # "2.00%" → strip "%" → "2.00" > 1 → divide by 100 → 0.02 → WARNING
        assert _severity_from_pct("2.00%") == "WARNING"

    def test_percent_format_values_lte_1_treated_as_ratio(self) -> None:
        # "0.50%" → strip "%" → "0.50" NOT > 1 → treated as ratio 0.50 → CRITICAL
        # (the function only converts values > 1 assuming they are percentage points)
        assert _severity_from_pct("0.50%") == "CRITICAL"

    def test_invalid_string_returns_ok(self) -> None:
        assert _severity_from_pct("not-a-number") == "OK"

    def test_none_returns_ok(self) -> None:
        assert _severity_from_pct(None) == "OK"

    def test_decimal_input(self) -> None:
        assert _severity_from_pct(Decimal("0.06")) == "CRITICAL"


# ---------------------------------------------------------------------------
# evaluate_position_discrepancy
# ---------------------------------------------------------------------------


class TestEvaluatePositionDiscrepancy:
    def test_below_threshold_returns_empty(self) -> None:
        result = evaluate_position_discrepancy(
            {
                "deviation_id": "d1",
                "venue": "binance",
                "instrument": "BTC-USDT",
                "discrepancy": "0.001",
                "discrepancy_pct": "0.005",
            }
        )
        assert result == []

    def test_warning_severity_alert_shape(self) -> None:
        result = evaluate_position_discrepancy(
            {
                "deviation_id": "d2",
                "venue": "binance",
                "instrument": "ETH-USDT",
                "discrepancy": "0.01",
                "discrepancy_pct": "0.02",
            }
        )
        assert len(result) == 1
        alert = result[0]
        assert alert["severity"] == "WARNING"
        assert alert["rule_id"] == "position_qty_discrepancy"
        assert alert["venue"] == "binance"
        assert alert["instrument"] == "ETH-USDT"
        assert alert["delivered"] is False
        assert alert["delivery_channel"] == "telegram"
        assert "recon-pos-d2" in str(alert["alert_id"])

    def test_critical_severity_delivery_channel(self) -> None:
        result = evaluate_position_discrepancy(
            {
                "deviation_id": "d3",
                "venue": "okx",
                "instrument": "BTC-USDT",
                "discrepancy": "1.0",
                "discrepancy_pct": "0.10",
            }
        )
        assert len(result) == 1
        assert result[0]["severity"] == "CRITICAL"
        assert result[0]["delivery_channel"] == "pagerduty+telegram"

    def test_missing_keys_default_gracefully(self) -> None:
        result = evaluate_position_discrepancy({"discrepancy_pct": "0.02"})
        assert len(result) == 1
        assert result[0]["venue"] == ""
        assert result[0]["instrument"] == ""


# ---------------------------------------------------------------------------
# evaluate_balance_discrepancy
# ---------------------------------------------------------------------------


class TestEvaluateBalanceDiscrepancy:
    def test_match_status_returns_empty(self) -> None:
        result = evaluate_balance_discrepancy(
            {"venue": "binance", "currency": "USDT", "status": "MATCH", "discrepancy_total": "0"}
        )
        assert result == []

    def test_match_status_case_insensitive(self) -> None:
        result = evaluate_balance_discrepancy({"status": "match"})
        assert result == []

    def test_warning_alert_shape(self) -> None:
        result = evaluate_balance_discrepancy(
            {
                "venue": "bybit",
                "currency": "BTC",
                "status": "WARNING",
                "discrepancy_total": "0.5",
                "discrepancy_pct": "2.0%",
            }
        )
        assert len(result) == 1
        alert = result[0]
        assert alert["severity"] == "WARNING"
        assert alert["venue"] == "bybit"
        assert alert["rule_id"] == "balance_discrepancy"
        assert alert["delivery_channel"] == "telegram"
        assert "recon-bal-bybit-BTC" in str(alert["alert_id"])

    def test_critical_alert_shape(self) -> None:
        result = evaluate_balance_discrepancy(
            {
                "venue": "kraken",
                "currency": "ETH",
                "status": "CRITICAL",
                "discrepancy_total": "5.0",
                "discrepancy_pct": "10.0%",
            }
        )
        assert len(result) == 1
        assert result[0]["severity"] == "CRITICAL"
        assert result[0]["delivery_channel"] == "pagerduty+telegram"

    def test_unknown_status_treated_as_warning(self) -> None:
        result = evaluate_balance_discrepancy({"status": "SOME_OTHER_STATUS", "venue": "v", "currency": "c"})
        assert len(result) == 1
        assert result[0]["severity"] == "WARNING"


# ---------------------------------------------------------------------------
# evaluate_pnl_discrepancy
# ---------------------------------------------------------------------------


class TestEvaluatePnlDiscrepancy:
    def test_below_threshold_returns_empty(self) -> None:
        result = evaluate_pnl_discrepancy(
            {
                "venue": "deribit",
                "instrument": "BTC-PERP",
                "unexplained_pnl": "1.0",
                "unexplained_pct": "0.005",
            }
        )
        assert result == []

    def test_warning_alert_shape(self) -> None:
        result = evaluate_pnl_discrepancy(
            {
                "venue": "ftx",
                "instrument": "ETH-PERP",
                "unexplained_pnl": "10.0",
                "unexplained_pct": "0.02",
            }
        )
        assert len(result) == 1
        alert = result[0]
        assert alert["severity"] == "WARNING"
        assert alert["rule_id"] == "unexplained_pnl"
        assert alert["venue"] == "ftx"
        assert alert["instrument"] == "ETH-PERP"
        assert alert["delivery_channel"] == "telegram"
        assert "recon-pnl-ftx-ETH-PERP" in str(alert["alert_id"])

    def test_critical_alert_shape(self) -> None:
        result = evaluate_pnl_discrepancy(
            {
                "venue": "binance",
                "instrument": "SOL-PERP",
                "unexplained_pnl": "100.0",
                "unexplained_pct": "0.10",
            }
        )
        assert len(result) == 1
        assert result[0]["severity"] == "CRITICAL"
        assert result[0]["delivery_channel"] == "pagerduty+telegram"

    def test_delivered_field_is_false(self) -> None:
        result = evaluate_pnl_discrepancy({"unexplained_pct": "0.06"})
        assert len(result) == 1
        assert result[0]["delivered"] is False

    def test_created_at_is_iso_format(self) -> None:
        result = evaluate_pnl_discrepancy({"unexplained_pct": "0.06"})
        assert len(result) == 1
        datetime.fromisoformat(str(result[0]["created_at"]))
