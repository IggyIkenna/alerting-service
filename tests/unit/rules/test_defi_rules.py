"""Unit tests for DeFi alert rules."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from alerting_service.rules.defi_rules import (
    DefiAlertType,
    check_aave_utilization,
    check_feature_staleness,
    check_funding_rate_flip,
    check_health_factor,
    check_weeth_depeg,
    route_defi_alert,
)

# ---------------------------------------------------------------------------
# Health factor critical
# ---------------------------------------------------------------------------


class TestHealthFactor:
    """Threshold bands come from UAC LIQUIDATION_PARAMS_REGISTRY[AAVE_V3]:
    warning=1.30, critical=1.15, severe=1.05, liquidation=1.00. DefiAlert.severity
    collapses severe + liquidation into ``critical`` (MarginEvent retains the
    full ladder).
    """

    def test_in_critical_band_returns_critical(self) -> None:
        alert = check_health_factor(
            health_factor=Decimal("1.10"),
            protocol="aave_v3",
            position_id="pos_001",
            asset="weETH",
        )
        assert alert is not None
        assert alert.alert_type == DefiAlertType.HEALTH_FACTOR_CRITICAL
        assert alert.severity == "critical"
        assert alert.protocol == "aave_v3"
        assert alert.asset == "weETH"

    def test_below_severe_is_critical(self) -> None:
        alert = check_health_factor(
            health_factor=Decimal("1.02"),
            protocol="aave_v3",
            position_id="pos_002",
            asset="ETH",
        )
        assert alert is not None
        assert alert.severity == "critical"

    def test_at_critical_threshold_is_warning(self) -> None:
        # HF == critical(1.15) -- still below warning band, mapped to "warning"
        # per `if HF >= critical: warning`.
        alert = check_health_factor(
            health_factor=Decimal("1.15"),
            protocol="aave_v3",
            position_id="pos_003",
            asset="ETH",
        )
        assert alert is not None
        assert alert.severity == "warning"

    def test_in_warning_band_is_warning(self) -> None:
        alert = check_health_factor(
            health_factor=Decimal("1.20"),
            protocol="aave_v3",
            position_id="pos_004",
            asset="ETH",
        )
        assert alert is not None
        assert alert.severity == "warning"

    def test_above_warning_threshold_returns_none(self) -> None:
        alert = check_health_factor(
            health_factor=Decimal("1.5"),
            protocol="aave_v3",
            position_id="pos_005",
            asset="weETH",
        )
        assert alert is None

    def test_at_warning_threshold_returns_none(self) -> None:
        alert = check_health_factor(
            health_factor=Decimal("1.30"),
            protocol="aave_v3",
            position_id="pos_006",
            asset="weETH",
        )
        assert alert is None

    def test_compound_protocol_uses_compound_thresholds(self) -> None:
        alert = check_health_factor(
            health_factor=Decimal("1.10"),
            protocol="compound_v3",
            position_id="pos_007",
            asset="USDC",
        )
        assert alert is not None
        assert alert.severity == "critical"

    def test_unknown_protocol_returns_none(self) -> None:
        alert = check_health_factor(
            health_factor=Decimal("0.01"),
            protocol="unknown_protocol",
            position_id="pos_008",
            asset="ETH",
        )
        assert alert is None


# ---------------------------------------------------------------------------
# weETH depeg
# ---------------------------------------------------------------------------


class TestWeethDepeg:
    def test_significant_depeg_triggers_alert(self) -> None:
        alert = check_weeth_depeg(weeth_eth_rate=Decimal("0.97"))
        assert alert is not None
        assert alert.alert_type == DefiAlertType.WEETH_DEPEG
        assert alert.asset == "weETH"

    def test_upward_depeg_triggers_alert(self) -> None:
        alert = check_weeth_depeg(weeth_eth_rate=Decimal("1.03"))
        assert alert is not None
        assert alert.alert_type == DefiAlertType.WEETH_DEPEG

    def test_severe_depeg_is_critical(self) -> None:
        alert = check_weeth_depeg(weeth_eth_rate=Decimal("0.93"))
        assert alert is not None
        assert alert.severity == "critical"

    def test_within_threshold_returns_none(self) -> None:
        alert = check_weeth_depeg(weeth_eth_rate=Decimal("0.99"))
        assert alert is None

    def test_exactly_at_threshold_returns_none(self) -> None:
        # 2% deviation from 1.0 = 0.98, which is <= threshold
        alert = check_weeth_depeg(weeth_eth_rate=Decimal("0.98"))
        assert alert is None

    def test_perfect_peg_returns_none(self) -> None:
        alert = check_weeth_depeg(weeth_eth_rate=Decimal("1.0"))
        assert alert is None


# ---------------------------------------------------------------------------
# Aave utilization spike
# ---------------------------------------------------------------------------


class TestAaveUtilization:
    def test_high_utilization_triggers_alert(self) -> None:
        alert = check_aave_utilization(
            utilization_rate=Decimal("0.98"),
            pool_name="USDC",
        )
        assert alert is not None
        assert alert.alert_type == DefiAlertType.AAVE_UTILIZATION_SPIKE
        assert alert.asset == "USDC"
        assert "98.0%" in alert.message

    def test_custom_protocol(self) -> None:
        alert = check_aave_utilization(
            utilization_rate=Decimal("0.97"),
            pool_name="ETH",
            protocol="aave_v2",
        )
        assert alert is not None
        assert alert.protocol == "aave_v2"

    def test_below_threshold_returns_none(self) -> None:
        alert = check_aave_utilization(
            utilization_rate=Decimal("0.90"),
            pool_name="USDC",
        )
        assert alert is None

    def test_exactly_at_threshold_returns_none(self) -> None:
        alert = check_aave_utilization(
            utilization_rate=Decimal("0.95"),
            pool_name="USDC",
        )
        assert alert is None


# ---------------------------------------------------------------------------
# Funding rate flip
# ---------------------------------------------------------------------------


class TestFundingRateFlip:
    def test_negative_rate_on_short_triggers_alert(self) -> None:
        alert = check_funding_rate_flip(
            funding_rate=Decimal("-0.001"),
            position_side="short",
            symbol="ETH-USD",
        )
        assert alert is not None
        assert alert.alert_type == DefiAlertType.FUNDING_RATE_FLIP
        assert alert.asset == "ETH-USD"
        assert "shorts paying longs" in alert.message

    def test_negative_rate_on_long_returns_none(self) -> None:
        alert = check_funding_rate_flip(
            funding_rate=Decimal("-0.001"),
            position_side="long",
            symbol="ETH-USD",
        )
        assert alert is None

    def test_positive_rate_on_short_returns_none(self) -> None:
        alert = check_funding_rate_flip(
            funding_rate=Decimal("0.001"),
            position_side="short",
            symbol="ETH-USD",
        )
        assert alert is None

    def test_zero_rate_on_short_returns_none(self) -> None:
        alert = check_funding_rate_flip(
            funding_rate=Decimal("0"),
            position_side="short",
            symbol="ETH-USD",
        )
        assert alert is None

    def test_custom_venue(self) -> None:
        alert = check_funding_rate_flip(
            funding_rate=Decimal("-0.002"),
            position_side="short",
            symbol="BTC-USD",
            venue="dydx",
        )
        assert alert is not None
        assert alert.protocol == "dydx"


# ---------------------------------------------------------------------------
# Feature staleness
# ---------------------------------------------------------------------------


class TestFeatureStaleness:
    def test_stale_feature_triggers_alert(self) -> None:
        alert = check_feature_staleness(
            feature_name="aave_health_factor",
            age_seconds=300.0,
            sla_seconds=60.0,
        )
        assert alert is not None
        assert alert.alert_type == DefiAlertType.FEATURE_STALE
        assert "aave_health_factor" in alert.message
        assert "5.0x" in alert.message

    def test_within_sla_returns_none(self) -> None:
        alert = check_feature_staleness(
            feature_name="aave_health_factor",
            age_seconds=90.0,
            sla_seconds=60.0,
        )
        assert alert is None

    def test_exactly_2x_sla_returns_none(self) -> None:
        alert = check_feature_staleness(
            feature_name="weeth_rate",
            age_seconds=120.0,
            sla_seconds=60.0,
        )
        assert alert is None

    def test_zero_sla_returns_none(self) -> None:
        alert = check_feature_staleness(
            feature_name="broken_feature",
            age_seconds=100.0,
            sla_seconds=0.0,
        )
        assert alert is None

    def test_negative_sla_returns_none(self) -> None:
        alert = check_feature_staleness(
            feature_name="broken_feature",
            age_seconds=100.0,
            sla_seconds=-1.0,
        )
        assert alert is None


# ---------------------------------------------------------------------------
# Alert routing integration
# ---------------------------------------------------------------------------


class TestRouteDefiAlert:
    @patch("alerting_service.rules.defi_rules.route_event")
    @patch("alerting_service.rules.defi_rules.log_event")
    def test_routes_health_factor_alert(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        alert = check_health_factor(
            health_factor=Decimal("1.1"),
            protocol="aave_v3",
            position_id="pos_001",
            asset="weETH",
        )
        assert alert is not None
        route_defi_alert(alert)

        mock_route_event.assert_called_once()
        call_args = mock_route_event.call_args
        assert call_args[0][0] == "DEFI_HEALTH_FACTOR_CRITICAL"
        details = call_args[0][1]
        assert details["source"] == "alerting-service/defi-rules"
        assert details["protocol"] == "aave_v3"

    @patch("alerting_service.rules.defi_rules.route_event")
    @patch("alerting_service.rules.defi_rules.log_event")
    def test_routes_weeth_depeg_alert(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        alert = check_weeth_depeg(weeth_eth_rate=Decimal("0.96"))
        assert alert is not None
        route_defi_alert(alert)

        mock_route_event.assert_called_once()
        assert mock_route_event.call_args[0][0] == "DEFI_WEETH_DEPEG"

    @patch("alerting_service.rules.defi_rules.route_event")
    @patch("alerting_service.rules.defi_rules.log_event")
    def test_routes_utilization_alert(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        alert = check_aave_utilization(
            utilization_rate=Decimal("0.97"),
            pool_name="ETH",
        )
        assert alert is not None
        route_defi_alert(alert)

        mock_route_event.assert_called_once()
        assert mock_route_event.call_args[0][0] == "DEFI_AAVE_UTILIZATION_SPIKE"

    @patch("alerting_service.rules.defi_rules.route_event")
    @patch("alerting_service.rules.defi_rules.log_event")
    def test_routes_funding_rate_alert(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        alert = check_funding_rate_flip(
            funding_rate=Decimal("-0.001"),
            position_side="short",
            symbol="ETH-USD",
        )
        assert alert is not None
        route_defi_alert(alert)

        mock_route_event.assert_called_once()
        assert mock_route_event.call_args[0][0] == "DEFI_FUNDING_RATE_FLIP"

    @patch("alerting_service.rules.defi_rules.route_event")
    @patch("alerting_service.rules.defi_rules.log_event")
    def test_routes_feature_stale_alert(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        alert = check_feature_staleness(
            feature_name="aave_health_factor",
            age_seconds=300.0,
            sla_seconds=60.0,
        )
        assert alert is not None
        route_defi_alert(alert)

        mock_route_event.assert_called_once()
        assert mock_route_event.call_args[0][0] == "DEFI_FEATURE_STALE"

    @patch("alerting_service.rules.defi_rules.route_event")
    @patch("alerting_service.rules.defi_rules.log_event")
    def test_emits_defi_alert_routed_event(
        self,
        mock_log_event: MagicMock,
        mock_route_event: MagicMock,
    ) -> None:
        alert = check_weeth_depeg(weeth_eth_rate=Decimal("0.96"))
        assert alert is not None
        route_defi_alert(alert)

        mock_log_event.assert_called_once_with(
            "DEFI_ALERT_ROUTED",
            details={
                "event_name": "DEFI_WEETH_DEPEG",
                "alert_type": DefiAlertType.WEETH_DEPEG,
            },
        )
