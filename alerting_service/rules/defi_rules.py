"""DeFi-specific alert rules.

Alert types:
- Health factor critical (Aave liquidation risk when health_factor < 1.2)
- weETH depeg (weETH/ETH rate deviates > 2% from 1.0)
- Aave utilization spike (pool utilization > 95%)
- Funding rate flip (Hyperliquid funding goes negative on short positions)
- Feature staleness (DeFi feature age exceeds 2x its SLA)

Routing matrix
--------------

| Alert                 | Event name                      | Priority | Channel(s)           |
|-----------------------|---------------------------------|----------|----------------------|
| Health factor critical| DEFI_HEALTH_FACTOR_CRITICAL     | P0       | PagerDuty + Telegram |
| weETH depeg           | DEFI_WEETH_DEPEG                | P0       | PagerDuty + Telegram |
| Aave utilization spike| DEFI_AAVE_UTILIZATION_SPIKE     | P1       | Telegram             |
| Funding rate flip     | DEFI_FUNDING_RATE_FLIP          | P1       | Telegram             |
| Feature staleness     | DEFI_FEATURE_STALE              | P1       | Telegram             |

Service event taxonomy:
  SERVICE_EVENT: DEFI_ALERT_ROUTED
  SERVICE_EVENT: DEFI_ALERT_FAILED
"""

from __future__ import annotations

import logging
from decimal import Decimal

from unified_api_contracts import DefiAlertType
from unified_trading_library import log_event
from unified_internal_contracts import DefiAlert

from ..notifiers.router import route_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health factor critical (Aave liquidation risk)
# ---------------------------------------------------------------------------

_HEALTH_FACTOR_CRITICAL_THRESHOLD = Decimal("1.2")


def check_health_factor(
    health_factor: Decimal,
    protocol: str,
    position_id: str,
    asset: str,
) -> DefiAlert | None:
    """Generate P0 alert if Aave health factor drops below 1.2.

    Args:
        health_factor: Current health factor of the position.
        protocol: DeFi protocol name (e.g. "aave_v3").
        position_id: Unique identifier for the lending position.
        asset: Collateral asset symbol (e.g. "weETH").

    Returns:
        DefiAlert if health_factor < 1.2, None otherwise.
    """
    if health_factor >= _HEALTH_FACTOR_CRITICAL_THRESHOLD:
        return None
    severity = "critical" if health_factor < Decimal("1.05") else "warning"
    return DefiAlert(
        alert_type=DefiAlertType.HEALTH_FACTOR_CRITICAL,
        severity=severity,
        protocol=protocol,
        asset=asset,
        message=(
            f"Health factor {health_factor} on {protocol} position {position_id}"
            f" (asset={asset}) — liquidation risk"
        ),
        details={
            "health_factor": float(health_factor),
            "protocol": protocol,
            "position_id": position_id,
            "asset": asset,
        },
    )


# ---------------------------------------------------------------------------
# weETH depeg
# ---------------------------------------------------------------------------

_WEETH_DEPEG_THRESHOLD_PCT = Decimal("2.0")


def check_weeth_depeg(
    weeth_eth_rate: Decimal,
) -> DefiAlert | None:
    """Generate P0 alert if weETH/ETH rate deviates > 2% from 1.0.

    Args:
        weeth_eth_rate: Current weETH/ETH exchange rate.

    Returns:
        DefiAlert if deviation exceeds threshold, None otherwise.
    """
    deviation_pct = abs(weeth_eth_rate - Decimal("1.0")) * Decimal("100")
    if deviation_pct <= _WEETH_DEPEG_THRESHOLD_PCT:
        return None
    severity = "critical" if deviation_pct > Decimal("5.0") else "warning"
    return DefiAlert(
        alert_type=DefiAlertType.WEETH_DEPEG,
        severity=severity,
        protocol="lido_wrapped",
        asset="weETH",
        message=f"weETH depeg detected: rate={weeth_eth_rate} (deviation={deviation_pct}%)",
        details={
            "weeth_eth_rate": float(weeth_eth_rate),
            "deviation_pct": float(deviation_pct),
        },
    )


# ---------------------------------------------------------------------------
# Aave utilization spike
# ---------------------------------------------------------------------------

_AAVE_UTILIZATION_THRESHOLD = Decimal("0.95")


def check_aave_utilization(
    utilization_rate: Decimal,
    pool_name: str,
    protocol: str = "aave_v3",
) -> DefiAlert | None:
    """Generate P1 alert if Aave pool utilization exceeds 95%.

    Args:
        utilization_rate: Current utilization rate (0.0 to 1.0).
        pool_name: Name of the Aave lending pool (e.g. "USDC", "ETH").
        protocol: Protocol name (defaults to "aave_v3").

    Returns:
        DefiAlert if utilization > 0.95, None otherwise.
    """
    if utilization_rate <= _AAVE_UTILIZATION_THRESHOLD:
        return None
    return DefiAlert(
        alert_type=DefiAlertType.AAVE_UTILIZATION_SPIKE,
        severity="warning",
        protocol=protocol,
        asset=pool_name,
        message=(
            f"Aave utilization spike: {pool_name} pool at"
            f" {float(utilization_rate) * 100:.1f}% on {protocol}"
        ),
        details={
            "utilization_rate": float(utilization_rate),
            "pool_name": pool_name,
            "protocol": protocol,
        },
    )


# ---------------------------------------------------------------------------
# Funding rate flip (Hyperliquid)
# ---------------------------------------------------------------------------


def check_funding_rate_flip(
    funding_rate: Decimal,
    position_side: str,
    symbol: str,
    venue: str = "hyperliquid",
) -> DefiAlert | None:
    """Generate P1 alert when funding rate flips negative on a short position.

    A negative funding rate means shorts pay longs — adverse for short holders.

    Args:
        funding_rate: Current funding rate (negative = shorts pay longs).
        position_side: Position direction ("short" or "long").
        symbol: Trading pair symbol (e.g. "ETH-USD").
        venue: Venue name (defaults to "hyperliquid").

    Returns:
        DefiAlert if funding rate is negative and position is short, None otherwise.
    """
    if position_side != "short" or funding_rate >= Decimal("0"):
        return None
    return DefiAlert(
        alert_type=DefiAlertType.FUNDING_RATE_FLIP,
        severity="warning",
        protocol=venue,
        asset=symbol,
        message=(
            f"Funding rate flip: {symbol} rate={funding_rate} on {venue} — shorts paying longs"
        ),
        details={
            "funding_rate": float(funding_rate),
            "position_side": position_side,
            "symbol": symbol,
            "venue": venue,
        },
    )


# ---------------------------------------------------------------------------
# Feature staleness
# ---------------------------------------------------------------------------


def check_feature_staleness(
    feature_name: str,
    age_seconds: float,
    sla_seconds: float,
) -> DefiAlert | None:
    """Generate P1 alert when a DeFi feature's age exceeds 2x its SLA.

    Args:
        feature_name: Name of the DeFi feature (e.g. "aave_health_factor").
        age_seconds: Current age of the feature data in seconds.
        sla_seconds: Expected maximum age per the feature's SLA.

    Returns:
        DefiAlert if age > 2x SLA, None otherwise.
    """
    if sla_seconds <= 0 or age_seconds <= 2.0 * sla_seconds:
        return None
    staleness_ratio = age_seconds / sla_seconds
    return DefiAlert(
        alert_type=DefiAlertType.FEATURE_STALE,
        severity="warning",
        message=(
            f"DeFi feature stale: {feature_name} age={age_seconds:.0f}s"
            f" (SLA={sla_seconds:.0f}s, ratio={staleness_ratio:.1f}x)"
        ),
        details={
            "feature_name": feature_name,
            "age_seconds": age_seconds,
            "sla_seconds": sla_seconds,
            "staleness_ratio": staleness_ratio,
        },
    )


# ---------------------------------------------------------------------------
# Event routing
# ---------------------------------------------------------------------------

# Map alert types to canonical event names for the router.
_ALERT_TYPE_TO_EVENT: dict[DefiAlertType, str] = {
    DefiAlertType.HEALTH_FACTOR_CRITICAL: "DEFI_HEALTH_FACTOR_CRITICAL",
    DefiAlertType.WEETH_DEPEG: "DEFI_WEETH_DEPEG",
    DefiAlertType.AAVE_UTILIZATION_SPIKE: "DEFI_AAVE_UTILIZATION_SPIKE",
    DefiAlertType.FUNDING_RATE_FLIP: "DEFI_FUNDING_RATE_FLIP",
    DefiAlertType.FEATURE_STALE: "DEFI_FEATURE_STALE",
}


def route_defi_alert(alert: DefiAlert) -> None:
    """Route a DeFi alert through the standard event router.

    Converts the DefiAlert into an event_name + details dict and delegates
    to ``route_event`` which handles channel selection (PagerDuty, Telegram)
    based on the config-driven routing rules.

    Args:
        alert: The DeFi alert to route.
    """
    event_name = _ALERT_TYPE_TO_EVENT.get(alert.alert_type, f"DEFI_{alert.alert_type.upper()}")

    details: dict[str, object] = {
        "message": alert.message,
        "severity": alert.severity,
        "source": "alerting-service/defi-rules",
    }
    if alert.protocol is not None:
        details["protocol"] = alert.protocol
    if alert.asset is not None:
        details["asset"] = alert.asset
    details.update(alert.details)

    log_event(
        "DEFI_ALERT_ROUTED", details={"event_name": event_name, "alert_type": alert.alert_type}
    )

    route_event(event_name, details)
