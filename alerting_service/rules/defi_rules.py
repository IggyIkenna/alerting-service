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
| Rate deviation        | DEFI_RATE_DEVIATION             | P0/P1    | PagerDuty + Telegram |
| Feature staleness     | DEFI_FEATURE_STALE              | P1       | Telegram             |
| TX simulation failed  | DEFI_TX_SIMULATION_FAILED       | P1       | Telegram             |
| Position liquidated   | DEFI_POSITION_LIQUIDATED        | P0       | PagerDuty + Telegram |

Service event taxonomy:
  SERVICE_EVENT: DEFI_ALERT_ROUTED
  SERVICE_EVENT: DEFI_ALERT_FAILED
"""

from __future__ import annotations

import logging
from decimal import Decimal

from unified_api_contracts import ALERT_THRESHOLDS, AlertCode, DefiAlertType, ThresholdUnit
from unified_api_contracts.internal import (  # noqa: qg-deep-import
    DefiAlert,
    MarginModel,
    get_liquidation_params,
)
from unified_trading_library import log_event

from ..notifiers.router import route_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health factor critical (DeFi liquidation risk)
# ---------------------------------------------------------------------------
#
# Thresholds come from UAC ``LIQUIDATION_PARAMS_REGISTRY`` — never inlined.
# Three previously-conflicting Aave HF threshold sets (risk-and-exposure
# 1.5/1.3/1.1, alerting 1.2, strategy 1.2) are reconciled to one canonical
# entry: warning=1.30, critical=1.15, severe=1.05, liquidation=1.00.

_PROTOCOL_TO_MARGIN_MODEL: dict[str, MarginModel] = {
    "aave_v3": MarginModel.AAVE_V3,
    "compound_v3": MarginModel.COMPOUND_V3,
    "morpho_blue": MarginModel.MORPHO_BLUE,
}


def _classify_health_factor(
    health_factor: Decimal,
    protocol: str,
) -> str | None:
    """Return alert severity for a health factor, or None if no alert.

    Severity ladder is read from UAC ``LIQUIDATION_PARAMS_REGISTRY``:
      HF >= warning           -> None (no alert)
      HF in [critical,warning) -> "warning"
      HF in [severe,critical)  -> "critical"
      HF < severe              -> "critical"   (severe + liquidation collapse to
                                                "critical" for DefiAlert.severity
                                                which only carries warning/critical;
                                                MarginEvent retains the full ladder)
    """
    margin_model = _PROTOCOL_TO_MARGIN_MODEL.get(protocol.lower())
    if margin_model is None:
        return None
    params = get_liquidation_params(margin_model)
    warning = params.health_factor_warning
    critical = params.health_factor_critical
    if warning is None or critical is None:
        return None
    if health_factor >= warning:
        return None
    if health_factor >= critical:
        return "warning"
    return "critical"


def check_health_factor(
    health_factor: Decimal,
    protocol: str,
    position_id: str,
    asset: str,
) -> DefiAlert | None:
    """Generate alert when DeFi health factor breaches the canonical band.

    Thresholds come from UAC ``LIQUIDATION_PARAMS_REGISTRY[protocol]``.

    Args:
        health_factor: Current health factor of the position.
        protocol: DeFi protocol name (e.g. "aave_v3", "compound_v3", "morpho_blue").
        position_id: Unique identifier for the lending position.
        asset: Collateral asset symbol (e.g. "weETH").

    Returns:
        DefiAlert if health_factor breaches the warning band, None otherwise.
    """
    severity = _classify_health_factor(health_factor, protocol)
    if severity is None:
        return None
    return DefiAlert(
        alert_type=DefiAlertType.HEALTH_FACTOR_CRITICAL,
        severity=severity,
        protocol=protocol,
        asset=asset,
        message=(
            f"Health factor {health_factor} on {protocol} position {position_id} (asset={asset}) — liquidation risk"
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
        code=AlertCode.DEFI_WEETH_DEPEG,
    )


# ---------------------------------------------------------------------------
# Aave utilization spike
# ---------------------------------------------------------------------------
#
# Threshold comes from UAC ``ALERT_THRESHOLDS`` — never inlined. The legacy
# ``_AAVE_UTILIZATION_THRESHOLD = Decimal("0.95")`` was migrated to UAC in
# Phase 2 of ``alerting_service_live_rules_2026_05_07`` and now reads from
# ``ALERT_THRESHOLDS["defi_aave_utilization_spike_bps"]`` with explicit
# ``ThresholdUnit.BPS_OF_ONE`` (9500 bps_of_one = 95.00 %). Per-archetype
# overrides supported via ``threshold.for_archetype(archetype)``.


def _aave_utilization_threshold_ratio(archetype: str | None = None) -> Decimal:
    """Return the AAVE utilization threshold as a ratio (0.0 to 1.0)."""
    threshold = ALERT_THRESHOLDS["defi_aave_utilization_spike_bps"]
    if threshold.unit is not ThresholdUnit.BPS_OF_ONE:
        msg = (
            "defi_aave_utilization_spike_bps unit drifted from BPS_OF_ONE; "
            "alerting-service consumer expects bps_of_one normalisation"
        )
        raise ValueError(msg)
    bps = threshold.for_archetype(archetype)
    return bps / Decimal("10000")


def check_aave_utilization(
    utilization_rate: Decimal,
    pool_name: str,
    protocol: str = "aave_v3",
    archetype: str | None = None,
) -> DefiAlert | None:
    """Generate P1 alert if Aave pool utilization exceeds the UAC threshold.

    Args:
        utilization_rate: Current utilization rate (0.0 to 1.0).
        pool_name: Name of the Aave lending pool (e.g. "USDC", "ETH").
        protocol: Protocol name (defaults to "aave_v3").
        archetype: Optional strategy-archetype key for per-archetype overrides
            (e.g. "leveraged_funding_arb" gets a tighter 90 % threshold).

    Returns:
        DefiAlert if utilization > threshold, None otherwise.
    """
    threshold_ratio = _aave_utilization_threshold_ratio(archetype)
    if utilization_rate <= threshold_ratio:
        return None
    return DefiAlert(
        alert_type=DefiAlertType.AAVE_UTILIZATION_SPIKE,
        severity="warning",
        protocol=protocol,
        asset=pool_name,
        message=(
            f"Aave utilization spike: {pool_name} pool at"
            f" {float(utilization_rate) * 100:.1f}% on {protocol}"
            f" (threshold {float(threshold_ratio) * 100:.1f}%)"
        ),
        details={
            "utilization_rate": float(utilization_rate),
            "threshold_ratio": float(threshold_ratio),
            "pool_name": pool_name,
            "protocol": protocol,
            "archetype": archetype if archetype is not None else "default",
        },
        code=AlertCode.DEFI_AAVE_UTILIZATION_SPIKE,
    )


# ---------------------------------------------------------------------------
# Rate deviation (actual vs projected APY)
# ---------------------------------------------------------------------------

_RATE_DEVIATION_WARNING_BPS = Decimal("50")
_RATE_DEVIATION_CRITICAL_BPS = Decimal("200")


def check_rate_deviation(
    actual_apy: Decimal,
    projected_apy: Decimal,
    pool_name: str,
    rate_type: str,  # "supply" or "borrow"
    protocol: str = "aave_v3",
) -> DefiAlert | None:
    """Generate alert when actual APY deviates from projected APY.

    Deviation is measured in basis points. >50 bps triggers a P1 warning
    (Telegram only), >200 bps triggers a P0 critical alert (PagerDuty +
    Telegram).

    Args:
        actual_apy: Observed APY as a decimal (e.g. Decimal("0.0350") = 3.50%).
        projected_apy: Model-projected APY as a decimal.
        pool_name: Name of the lending pool (e.g. "USDC", "ETH").
        rate_type: Rate category — "supply" or "borrow".
        protocol: Protocol name (defaults to "aave_v3").

    Returns:
        DefiAlert if deviation > 50 bps, None otherwise.
    """
    deviation_bps = abs(actual_apy - projected_apy) * Decimal("10000")
    if deviation_bps <= _RATE_DEVIATION_WARNING_BPS:
        return None
    severity = "critical" if deviation_bps > _RATE_DEVIATION_CRITICAL_BPS else "warning"
    actual_pct = actual_apy * Decimal("100")
    projected_pct = projected_apy * Decimal("100")
    return DefiAlert(
        alert_type=DefiAlertType.RATE_DEVIATION,
        severity=severity,
        protocol=protocol,
        asset=pool_name,
        message=(
            f"Rate deviation on {pool_name} ({rate_type}): actual={actual_pct:.2f}%"
            f" projected={projected_pct:.2f}% deviation={deviation_bps:.0f}bps"
        ),
        details={
            "actual_apy": float(actual_apy),
            "projected_apy": float(projected_apy),
            "deviation_bps": float(deviation_bps),
            "rate_type": rate_type,
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
        message=(f"Funding rate flip: {symbol} rate={funding_rate} on {venue} — shorts paying longs"),
        details={
            "funding_rate": float(funding_rate),
            "position_side": position_side,
            "symbol": symbol,
            "venue": venue,
        },
        code=AlertCode.DEFI_FUNDING_RATE_FLIP,
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
        code=AlertCode.DEFI_FEATURE_STALE,
    )


# ---------------------------------------------------------------------------
# TX simulation failed (Tenderly pre-simulation revert)
# ---------------------------------------------------------------------------


def check_tx_simulation(
    success: bool,
    revert_reason: str,
    protocol: str,
    tx_type: str,
    estimated_gas: int,
) -> DefiAlert | None:
    """Check if a Tenderly transaction simulation failed.

    Args:
        success: Whether the simulation succeeded.
        revert_reason: Revert reason if simulation failed.
        protocol: DeFi protocol (e.g. AAVE_V3, UNISWAP_V3).
        tx_type: Transaction type (e.g. SWAP, LEND, BORROW).
        estimated_gas: Estimated gas from simulation.

    Returns:
        DefiAlert if simulation failed, None if succeeded.
    """
    if success:
        return None

    return DefiAlert(
        alert_type=DefiAlertType.TX_SIMULATION_FAILED,
        severity="P1",
        protocol=protocol,
        asset=None,
        message=(f"DeFi tx simulation failed: {tx_type} on {protocol} — revert: {revert_reason}"),
        details={
            "tx_type": tx_type,
            "revert_reason": revert_reason,
            "estimated_gas": estimated_gas,
        },
    )


# ---------------------------------------------------------------------------
# Position liquidated (on-chain Aave LiquidationCall)
# ---------------------------------------------------------------------------


def check_position_liquidated(
    liquidated: bool,
    protocol: str,
    wallet: str,
    collateral_asset: str,
    debt_asset: str,
    liquidated_collateral: Decimal,
    debt_covered: Decimal,
) -> DefiAlert | None:
    """Check if an on-chain liquidation was detected.

    Args:
        liquidated: Whether a liquidation event was detected.
        protocol: DeFi protocol (e.g. AAVE_V3).
        wallet: Wallet address that was liquidated.
        collateral_asset: Collateral asset seized.
        debt_asset: Debt asset repaid.
        liquidated_collateral: Amount of collateral seized.
        debt_covered: Amount of debt covered.

    Returns:
        DefiAlert if liquidation detected, None otherwise.
    """
    if not liquidated:
        return None

    return DefiAlert(
        alert_type=DefiAlertType.POSITION_LIQUIDATED,
        severity="P0",
        protocol=protocol,
        asset=collateral_asset,
        message=(
            f"DeFi position liquidated: {wallet[:10]}... on {protocol}"
            f" — collateral={collateral_asset} debt={debt_asset}"
            f" seized={liquidated_collateral}"
        ),
        details={
            "wallet": wallet,
            "collateral_asset": collateral_asset,
            "debt_asset": debt_asset,
            "liquidated_collateral": str(liquidated_collateral),
            "debt_covered": str(debt_covered),
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
    DefiAlertType.RATE_DEVIATION: "DEFI_RATE_DEVIATION",
    DefiAlertType.FUNDING_RATE_FLIP: "DEFI_FUNDING_RATE_FLIP",
    DefiAlertType.FEATURE_STALE: "DEFI_FEATURE_STALE",
    DefiAlertType.TX_SIMULATION_FAILED: "DEFI_TX_SIMULATION_FAILED",
    DefiAlertType.POSITION_LIQUIDATED: "DEFI_POSITION_LIQUIDATED",
}


def route_defi_alert(alert: DefiAlert) -> None:
    """Route a DeFi alert through the standard event router.

    Converts the DefiAlert into an event_name + details dict and delegates
    to ``route_event`` which handles channel selection (PagerDuty, Telegram)
    based on the config-driven routing rules.

    Args:
        alert: The DeFi alert to route.
    """
    # Prefer canonical AlertCode for routing when set (producer-migration window
    # 2026-05-08+, per Phase 3 of alerting_service_live_rules_2026_05_07.md).
    # Fall back to legacy DefiAlertType mapping when code is absent.
    if alert.code is not None:
        event_name = alert.code.value
    else:
        event_name = _ALERT_TYPE_TO_EVENT.get(alert.alert_type, f"DEFI_{alert.alert_type.upper()}")

    details: dict[str, object] = {
        "message": alert.message,
        "severity": alert.severity,
        "source": "alerting-service/defi-rules",
    }
    if alert.code is not None:
        details["code"] = alert.code.value
    if alert.protocol is not None:
        details["protocol"] = alert.protocol
    if alert.asset is not None:
        details["asset"] = alert.asset
    details.update(alert.details)

    log_event("DEFI_ALERT_ROUTED", details={"event_name": event_name, "alert_type": alert.alert_type})

    route_event(event_name, details)
