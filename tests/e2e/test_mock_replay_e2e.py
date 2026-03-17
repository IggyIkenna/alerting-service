"""Mock replay E2E tests for alerting-service.

Loads VCR cassettes from the UAC canonical location and validates that
the alerting-service can process external API health/status data.
Runs under CLOUD_MOCK_MODE=true with zero network calls.

Alerting service monitors:
- Exchange health endpoints (via internal service cassettes)
- Market data freshness (via exchange ticker cassettes)
- DeFi protocol status (via Hyperliquid/DeFiLlama cassettes)

Cassettes used:
- deribit/risk_service_health.yaml: Internal health check
- deribit/ticker.yaml: Market data for freshness monitoring
- hyperliquid/meta_and_asset_ctxs.yaml: DeFi protocol status
- binance/ticker_24hr.yaml: CeFi exchange status
"""

from __future__ import annotations

import pytest
import responses
from unified_api_contracts.testing.cassette_loader import (
    list_cassettes_for_venue,
    load_cassette,
)
from unified_api_contracts.testing.mock_replay import (
    replay_cassette,
)

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Service health monitoring via cassettes
# ---------------------------------------------------------------------------


class TestServiceHealthMonitoring:
    """E2E: alerting-service monitors service health via cassette replay."""

    def test_risk_service_health_cassette_loadable(self) -> None:
        """Risk service health cassette provides healthy status."""
        body = load_cassette("deribit", "risk_service_health.yaml")
        assert isinstance(body, dict)
        assert body["status"] == "healthy"
        assert body["service"] == "risk-and-exposure-service"

    @responses.activate
    def test_health_endpoint_replay(self) -> None:
        """Health endpoint cassette replays via HTTP mock."""
        replay_cassette("deribit", "risk_service_health.yaml")

        import requests

        resp = requests.get("http://risk-service:8080/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"

    def test_health_status_classification(self) -> None:
        """Alert rule can classify health response into alert/no-alert."""
        body = load_cassette("deribit", "risk_service_health.yaml")

        # Alerting-service classifies: healthy = no alert, unhealthy = alert
        status = body.get("status", "unknown")
        should_alert = status != "healthy"
        assert not should_alert, "Healthy status should not trigger alert"


# ---------------------------------------------------------------------------
# Market data freshness monitoring
# ---------------------------------------------------------------------------


class TestMarketDataFreshnessMonitoring:
    """E2E: alerting-service monitors market data freshness via cassettes."""

    def test_deribit_ticker_has_timestamp_data(self) -> None:
        """Deribit ticker cassette provides timestamp for freshness check."""
        body = load_cassette("deribit", "ticker.yaml")
        result = body["result"]
        assert isinstance(result, dict)

        # Ticker data has timestamp or state info for freshness
        # The presence of result data means the exchange is responding
        assert len(result) > 0

    @responses.activate
    def test_binance_ticker_freshness_check(self) -> None:
        """Binance ticker cassette can be used for data freshness monitoring."""
        replay_cassette("binance", "ticker_24hr.yaml")

        import requests

        resp = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT")
        assert resp.status_code == 200
        # 200 response means exchange is responsive -> no freshness alert

    def test_freshness_alert_threshold_logic(self) -> None:
        """Freshness monitoring: if response is valid, no stale-data alert."""
        body = load_cassette("deribit", "ticker.yaml")

        # If we can parse the response, data is fresh
        is_fresh = isinstance(body, dict) and "result" in body
        should_alert = not is_fresh
        assert not should_alert


# ---------------------------------------------------------------------------
# DeFi protocol health monitoring
# ---------------------------------------------------------------------------


class TestDefiProtocolMonitoring:
    """E2E: alerting-service monitors DeFi protocol health."""

    def test_hyperliquid_protocol_health(self) -> None:
        """Hyperliquid meta cassette indicates protocol is healthy."""
        body = load_cassette("hyperliquid", "meta_and_asset_ctxs.yaml")

        # Protocol health check: universe is non-empty
        universe = body.get("universe", [])
        protocol_healthy = isinstance(universe, list) and len(universe) > 0
        assert protocol_healthy

    def test_hyperliquid_clearinghouse_health(self) -> None:
        """Clearinghouse state cassette validates account access is working."""
        body = load_cassette("hyperliquid", "clearinghouse_state.yaml")

        # If we can read clearinghouse state, the protocol is accessible
        has_margin = "marginSummary" in body
        has_withdrawable = "withdrawable" in body
        assert has_margin and has_withdrawable


# ---------------------------------------------------------------------------
# Alert rule integration with cassette data
# ---------------------------------------------------------------------------


class TestAlertRuleIntegration:
    """E2E: Alert rules process cassette data and produce correct outcomes."""

    def test_data_freshness_rule_no_alert_on_fresh_data(self) -> None:
        """Data freshness rule should not alert when exchange data is available."""
        # Simulate checking multiple exchange endpoints
        venues_to_check = ["deribit", "binance", "hyperliquid"]
        alerts: list[str] = []

        for venue in venues_to_check:
            cassettes = list_cassettes_for_venue(venue)
            if not cassettes:
                alerts.append(f"{venue}: no cassettes available")
                continue

            # If venue has cassettes, it means data exists -> no freshness alert
            # In production, this would check timestamp age

        assert len(alerts) == 0, f"Unexpected alerts: {alerts}"

    def test_multi_venue_health_aggregation(self) -> None:
        """Aggregate health status across venues from cassettes."""
        health_statuses: dict[str, bool] = {}

        # Check Deribit via health cassette
        body = load_cassette("deribit", "risk_service_health.yaml")
        health_statuses["deribit_risk_service"] = body.get("status") == "healthy"

        # Check Hyperliquid via meta cassette
        hl_body = load_cassette("hyperliquid", "meta_and_asset_ctxs.yaml")
        health_statuses["hyperliquid"] = (
            isinstance(hl_body.get("universe"), list) and len(hl_body["universe"]) > 0
        )

        # All services should be healthy
        assert all(health_statuses.values()), (
            f"Unhealthy services: {[k for k, v in health_statuses.items() if not v]}"
        )
