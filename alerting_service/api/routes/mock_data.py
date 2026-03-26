"""Mock data for alerting-service routes when CLOUD_MOCK_MODE=true.

Provides realistic sample data for local development and testing without
requiring cloud storage backends.
"""

from __future__ import annotations

from datetime import UTC, datetime

from unified_api_contracts.internal import AlertEvent

_NOW = "2026-03-14T12:00:00"


def _make_alert_events() -> list[AlertEvent]:
    """Build mock AlertEvent instances for /rules/recent."""
    return [
        AlertEvent(
            alert_id="alert-001",
            rule_id="KILL_SWITCH_TRIGGERED",
            triggered_at=datetime(2026, 3, 14, 11, 55, 0, tzinfo=UTC),
            severity="CRITICAL",
            message="Kill switch activated: max drawdown -3.2% exceeded -3.0% threshold",
            metric_value=-3.2,
            threshold=-3.0,
            strategy_id="momentum-macd-v2",
            venue="binance",
        ),
        AlertEvent(
            alert_id="alert-002",
            rule_id="CIRCUIT_BREAKER_OPEN",
            triggered_at=datetime(2026, 3, 14, 11, 0, 0, tzinfo=UTC),
            severity="CRITICAL",
            message="Circuit breaker opened for execution-service: error rate 12% > 10%",
            metric_value=12.0,
            threshold=10.0,
            strategy_id=None,
            venue=None,
        ),
        AlertEvent(
            alert_id="alert-003",
            rule_id="DATA_STALE",
            triggered_at=datetime(2026, 3, 14, 9, 45, 0, tzinfo=UTC),
            severity="WARNING",
            message="Market data stale for binance BTC-USDT: last update 65s ago (threshold 30s)",
            metric_value=65.0,
            threshold=30.0,
            strategy_id=None,
            venue="binance",
        ),
        AlertEvent(
            alert_id="alert-004",
            rule_id="PREFLIGHT_FAILED",
            triggered_at=datetime(2026, 3, 14, 8, 30, 0, tzinfo=UTC),
            severity="WARNING",
            message=(
                "Preflight check failed for strategy breakout-v1:"
                " insufficient liquidity on bybit SOL-USDT"
            ),
            metric_value=0.0,
            threshold=1.0,
            strategy_id="breakout-v1",
            venue="bybit",
        ),
        AlertEvent(
            alert_id="alert-005",
            rule_id="SERVICE_DEGRADED",
            triggered_at=datetime(2026, 3, 14, 10, 15, 0, tzinfo=UTC),
            severity="WARNING",
            message="Strategy-service response latency p99=2.8s exceeds 2.0s SLO",
            metric_value=2.8,
            threshold=2.0,
            strategy_id=None,
            venue=None,
        ),
        AlertEvent(
            alert_id="alert-006",
            rule_id="POSITION_LIMIT_BREACH",
            triggered_at=datetime(2026, 3, 14, 9, 0, 0, tzinfo=UTC),
            severity="CRITICAL",
            message="Position limit 85% utilized on okx (threshold 80%)",
            metric_value=85.0,
            threshold=80.0,
            strategy_id=None,
            venue="okx",
        ),
        AlertEvent(
            alert_id="alert-007",
            rule_id="DATA_STALE",
            triggered_at=datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC),
            severity="WARNING",
            message="Instruments data stale for deribit: last refresh 3600s ago (threshold 1800s)",
            metric_value=3600.0,
            threshold=1800.0,
            strategy_id=None,
            venue="deribit",
        ),
        AlertEvent(
            alert_id="alert-008",
            rule_id="KILL_SWITCH_ARMED",
            triggered_at=datetime(2026, 3, 14, 7, 0, 0, tzinfo=UTC),
            severity="INFO",
            message="Kill switch armed for trading session 2026-03-14",
            metric_value=0.0,
            threshold=0.0,
            strategy_id=None,
            venue=None,
        ),
        AlertEvent(
            alert_id="alert-009",
            rule_id="MARGIN_WARNING",
            triggered_at=datetime(2026, 3, 14, 9, 30, 0, tzinfo=UTC),
            severity="WARNING",
            message="Margin utilization 78% on binance approaching 80% limit",
            metric_value=78.0,
            threshold=80.0,
            strategy_id=None,
            venue="binance",
        ),
        AlertEvent(
            alert_id="alert-010",
            rule_id="CIRCUIT_BREAKER_CLOSED",
            triggered_at=datetime(2026, 3, 14, 12, 30, 0, tzinfo=UTC),
            severity="INFO",
            message="Circuit breaker closed for execution-service: error rate recovered to 2%",
            metric_value=2.0,
            threshold=10.0,
            strategy_id=None,
            venue=None,
        ),
    ]


MOCK_RECENT_ALERTS: list[AlertEvent] = _make_alert_events()

MOCK_SSE_EVENTS: list[dict[str, str | float]] = [
    {
        "alert_id": "alert-sse-001",
        "rule_id": "DATA_STALE",
        "severity": "WARNING",
        "message": "Market data stale for okx ETH-USDT",
        "metric_value": 45.0,
        "threshold": 30.0,
    },
    {
        "alert_id": "alert-sse-002",
        "rule_id": "PREFLIGHT_FAILED",
        "severity": "WARNING",
        "message": "Preflight failed: risk check timeout",
        "metric_value": 0.0,
        "threshold": 1.0,
    },
    {
        "alert_id": "alert-sse-003",
        "rule_id": "KILL_SWITCH_TRIGGERED",
        "severity": "CRITICAL",
        "message": "Kill switch: daily loss limit reached",
        "metric_value": -5000.0,
        "threshold": -4500.0,
    },
]

MOCK_DELIVERY_RECORDS: list[dict[str, str | bool]] = [
    {
        "delivery_id": "del-001",
        "alert_id": "alert-001",
        "channel": "pagerduty",
        "status": "delivered",
        "delivered_at": "2026-03-14T11:55:02Z",
        "acknowledged": True,
    },
    {
        "delivery_id": "del-002",
        "alert_id": "alert-001",
        "channel": "telegram",
        "status": "delivered",
        "delivered_at": "2026-03-14T11:55:01Z",
        "acknowledged": False,
    },
    {
        "delivery_id": "del-003",
        "alert_id": "alert-001",
        "channel": "slack",
        "status": "failed",
        "delivered_at": "",
        "acknowledged": False,
    },
]
