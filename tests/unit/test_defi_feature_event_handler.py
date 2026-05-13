"""Unit tests for alerting_service.defi_feature_event_handler.

Verifies the producer→rule→router bridge for the 4 May-23 DeFi alert codes:
  DEFI_AAVE_UTILIZATION_SPIKE / DEFI_FUNDING_RATE_FLIP / DEFI_FEATURE_STALE /
  DEFI_WEETH_DEPEG.

Each test patches route_defi_alert to capture the routed alert without hitting
the real router (PagerDuty / Slack). Tests cover (a) firing path, (b) silent
no-op when value within bounds, (c) malformed payload safe-skip.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from alerting_service.defi_feature_event_handler import (
    DEFI_FEATURE_AAVE_UTILIZATION,
    DEFI_FEATURE_PERP_FUNDING_RATE,
    DEFI_FEATURE_STALENESS,
    DEFI_FEATURE_WEETH_ETH_RATE,
    handle_defi_feature_event,
)


def _route_target() -> str:
    return "alerting_service.defi_feature_event_handler.route_defi_alert"


@pytest.mark.unit
class TestDefiAaveUtilization:
    def test_fires_when_above_threshold(self) -> None:
        """Utilization 96 % (above 95 % default) → route_defi_alert called once."""
        with patch(_route_target()) as mock_route:
            handle_defi_feature_event(
                DEFI_FEATURE_AAVE_UTILIZATION,
                {"utilization_rate": 0.96, "pool_name": "USDC"},
            )
        assert mock_route.call_count == 1
        alert = mock_route.call_args.args[0]
        assert alert.code is not None
        assert alert.code.value == "DEFI_AAVE_UTILIZATION_SPIKE"

    def test_silent_below_threshold(self) -> None:
        """Utilization 80 % → no alert routed."""
        with patch(_route_target()) as mock_route:
            handle_defi_feature_event(
                DEFI_FEATURE_AAVE_UTILIZATION,
                {"utilization_rate": 0.80, "pool_name": "USDC"},
            )
        assert mock_route.call_count == 0

    def test_safe_skip_missing_field(self) -> None:
        """Payload missing pool_name → safe-skip with no alert."""
        with patch(_route_target()) as mock_route:
            handle_defi_feature_event(
                DEFI_FEATURE_AAVE_UTILIZATION,
                {"utilization_rate": 0.96},
            )
        assert mock_route.call_count == 0


@pytest.mark.unit
class TestDefiFundingRateFlip:
    def test_fires_on_negative_short(self) -> None:
        """Negative funding + short position → alert routed."""
        with patch(_route_target()) as mock_route:
            handle_defi_feature_event(
                DEFI_FEATURE_PERP_FUNDING_RATE,
                {"funding_rate": -0.01, "position_side": "short", "symbol": "ETH-USD"},
            )
        assert mock_route.call_count == 1
        alert = mock_route.call_args.args[0]
        assert alert.code is not None
        assert alert.code.value == "DEFI_FUNDING_RATE_FLIP"

    def test_silent_long_position(self) -> None:
        """Long position with negative rate → no alert (rule only fires for shorts)."""
        with patch(_route_target()) as mock_route:
            handle_defi_feature_event(
                DEFI_FEATURE_PERP_FUNDING_RATE,
                {"funding_rate": -0.01, "position_side": "long", "symbol": "ETH-USD"},
            )
        assert mock_route.call_count == 0


@pytest.mark.unit
class TestDefiWeethDepeg:
    def test_fires_on_depeg(self) -> None:
        """Rate 1.03 (3 % deviation, above 2 % threshold) → alert routed."""
        with patch(_route_target()) as mock_route:
            handle_defi_feature_event(
                DEFI_FEATURE_WEETH_ETH_RATE,
                {"weeth_eth_rate": 1.03},
            )
        assert mock_route.call_count == 1
        alert = mock_route.call_args.args[0]
        assert alert.code is not None
        assert alert.code.value == "DEFI_WEETH_DEPEG"

    def test_silent_on_peg(self) -> None:
        """Rate 1.005 (0.5 % deviation, within threshold) → no alert."""
        with patch(_route_target()) as mock_route:
            handle_defi_feature_event(
                DEFI_FEATURE_WEETH_ETH_RATE,
                {"weeth_eth_rate": 1.005},
            )
        assert mock_route.call_count == 0


@pytest.mark.unit
class TestDefiFeatureStaleness:
    def test_fires_above_2x_sla(self) -> None:
        """Age 1800 s > 2x SLA 600 s → alert routed."""
        with patch(_route_target()) as mock_route:
            handle_defi_feature_event(
                DEFI_FEATURE_STALENESS,
                {
                    "feature_name": "aave_health_factor",
                    "age_seconds": 1800.0,
                    "sla_seconds": 600.0,
                },
            )
        assert mock_route.call_count == 1
        alert = mock_route.call_args.args[0]
        assert alert.code is not None
        assert alert.code.value == "DEFI_FEATURE_STALE"

    def test_silent_within_sla(self) -> None:
        """Age 700 s < 2x SLA 600 s → no alert."""
        with patch(_route_target()) as mock_route:
            handle_defi_feature_event(
                DEFI_FEATURE_STALENESS,
                {
                    "feature_name": "aave_health_factor",
                    "age_seconds": 700.0,
                    "sla_seconds": 600.0,
                },
            )
        assert mock_route.call_count == 0


@pytest.mark.unit
def test_unrecognised_event_name_is_silent() -> None:
    """Unknown event_name → no error, no route. Defensive dispatch."""
    with patch(_route_target()) as mock_route:
        handle_defi_feature_event("UNKNOWN_EVENT", {"foo": "bar"})
    assert mock_route.call_count == 0
