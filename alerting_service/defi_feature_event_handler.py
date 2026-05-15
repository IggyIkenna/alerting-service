"""Consumer for DeFi feature events that trigger the 4 May-23 DeFi alert rules.

Bridges features-service (onchain family) raw feature_ready events to the DeFi
rule check_* functions in :mod:`.rules.defi_rules` and routes any returned
:class:`DefiAlert` through the standard event router.

Producer-side migration window (Phase 3 of
``alerting_service_live_rules_2026_05_07.md``): producers emit raw feature events
with the canonical event name + payload; this handler resolves to the right
check_* function, applies the UAC threshold, and routes only if the rule fires.

Maps feature events to the 4 DeFi alert codes pulled into May-23 cutover
(2026-05-13 operator direction, ``master_to_live_defi_2026_05_23.md`` § "pull 7
items"):

  Feature event                       → check_*                     → AlertCode
  -----                                 -----                          -----
  DEFI_FEATURE_AAVE_UTILIZATION       → check_aave_utilization      → DEFI_AAVE_UTILIZATION_SPIKE
  DEFI_FEATURE_PERP_FUNDING_RATE      → check_funding_rate_flip     → DEFI_FUNDING_RATE_FLIP
  DEFI_FEATURE_WEETH_ETH_RATE         → check_weeth_depeg           → DEFI_WEETH_DEPEG
  DEFI_FEATURE_STALENESS              → check_feature_staleness     → DEFI_FEATURE_STALE

Per-event payload contracts:

* ``DEFI_FEATURE_AAVE_UTILIZATION``: ``{"utilization_rate": float, "pool_name": str,
  "protocol": str?, "archetype": str?}``
* ``DEFI_FEATURE_PERP_FUNDING_RATE``: ``{"funding_rate": float, "position_side": str,
  "symbol": str, "venue": str?}``
* ``DEFI_FEATURE_WEETH_ETH_RATE``: ``{"weeth_eth_rate": float}``
* ``DEFI_FEATURE_STALENESS``: ``{"feature_name": str, "age_seconds": float,
  "sla_seconds": float}``

Service event taxonomy:
  SERVICE_EVENT: DEFI_FEATURE_EVENT_RECEIVED
  SERVICE_EVENT: DEFI_FEATURE_EVENT_DISPATCHED
  SERVICE_EVENT: DEFI_FEATURE_EVENT_NO_ALERT
  SERVICE_EVENT: DEFI_FEATURE_EVENT_INVALID
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Final

from unified_api_contracts.internal import DefiAlert  # noqa: qg-deep-import  # pyright: ignore[reportPrivateUsage]
from unified_trading_library import log_event

from .rules.defi_rules import (
    check_aave_utilization,
    check_feature_staleness,
    check_funding_rate_flip,
    check_weeth_depeg,
    route_defi_alert,
)

logger = logging.getLogger(__name__)

# Canonical feature-event names emitted by features-service onchain family.
# Producer side MUST emit using these exact event_name strings so the dispatch
# below resolves correctly.
DEFI_FEATURE_AAVE_UTILIZATION: Final[str] = "DEFI_FEATURE_AAVE_UTILIZATION"
DEFI_FEATURE_PERP_FUNDING_RATE: Final[str] = "DEFI_FEATURE_PERP_FUNDING_RATE"
DEFI_FEATURE_WEETH_ETH_RATE: Final[str] = "DEFI_FEATURE_WEETH_ETH_RATE"
DEFI_FEATURE_STALENESS: Final[str] = "DEFI_FEATURE_STALENESS"

DEFI_FEATURE_EVENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        DEFI_FEATURE_AAVE_UTILIZATION,
        DEFI_FEATURE_PERP_FUNDING_RATE,
        DEFI_FEATURE_WEETH_ETH_RATE,
        DEFI_FEATURE_STALENESS,
    }
)


def _coerce_decimal(value: object, field: str) -> Decimal | None:
    """Convert a payload field to Decimal; return None on missing/invalid."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        logger.warning("DEFI_FEATURE_EVENT_INVALID: field=%s value=%r", field, value)
        return None


def _coerce_float(value: object, field: str) -> float | None:
    """Convert a payload field to float; return None on missing/invalid."""
    if value is None:
        return None
    try:
        return float(str(value))
    except (ValueError, TypeError):
        logger.warning("DEFI_FEATURE_EVENT_INVALID: field=%s value=%r", field, value)
        return None


def _coerce_str(value: object, field: str, *, required: bool = True) -> str | None:
    """Convert a payload field to str; return None on missing if not required."""
    if value is None:
        if required:
            logger.warning("DEFI_FEATURE_EVENT_INVALID: missing required field=%s", field)
        return None
    return str(value)


def _build_aave_utilization_alert(payload: dict[str, object]) -> DefiAlert | None:
    utilization = _coerce_decimal(payload.get("utilization_rate"), "utilization_rate")
    pool_name = _coerce_str(payload.get("pool_name"), "pool_name")
    if utilization is None or pool_name is None:
        return None
    protocol = _coerce_str(payload.get("protocol"), "protocol", required=False) or "aave_v3"
    archetype = _coerce_str(payload.get("archetype"), "archetype", required=False)
    return check_aave_utilization(utilization, pool_name, protocol=protocol, archetype=archetype)


def _build_funding_rate_flip_alert(payload: dict[str, object]) -> DefiAlert | None:
    funding_rate = _coerce_decimal(payload.get("funding_rate"), "funding_rate")
    position_side = _coerce_str(payload.get("position_side"), "position_side")
    symbol = _coerce_str(payload.get("symbol"), "symbol")
    if funding_rate is None or position_side is None or symbol is None:
        return None
    venue = _coerce_str(payload.get("venue"), "venue", required=False) or "hyperliquid"
    return check_funding_rate_flip(funding_rate, position_side, symbol, venue=venue)


def _build_weeth_depeg_alert(payload: dict[str, object]) -> DefiAlert | None:
    rate = _coerce_decimal(payload.get("weeth_eth_rate"), "weeth_eth_rate")
    if rate is None:
        return None
    return check_weeth_depeg(rate)


def _build_feature_staleness_alert(payload: dict[str, object]) -> DefiAlert | None:
    feature_name = _coerce_str(payload.get("feature_name"), "feature_name")
    age = _coerce_float(payload.get("age_seconds"), "age_seconds")
    sla = _coerce_float(payload.get("sla_seconds"), "sla_seconds")
    if feature_name is None or age is None or sla is None:
        return None
    return check_feature_staleness(feature_name, age, sla)


# Closed-set dispatch table: event_name → builder function.
_BUILDERS = {
    DEFI_FEATURE_AAVE_UTILIZATION: _build_aave_utilization_alert,
    DEFI_FEATURE_PERP_FUNDING_RATE: _build_funding_rate_flip_alert,
    DEFI_FEATURE_WEETH_ETH_RATE: _build_weeth_depeg_alert,
    DEFI_FEATURE_STALENESS: _build_feature_staleness_alert,
}


def handle_defi_feature_event(event_name: str, payload: dict[str, object]) -> None:
    """Dispatch a DeFi feature event to the matching rule check + route any alert.

    Idempotent: if the rule does not fire (value within bounds), the function
    returns without side effect beyond a NO_ALERT lifecycle event.
    """
    log_event("DEFI_FEATURE_EVENT_RECEIVED", details={"event_name": event_name})
    builder = _BUILDERS.get(event_name)
    if builder is None:
        logger.debug("DEFI_FEATURE_EVENT_RECEIVED: unrecognised event_name=%s", event_name)
        return
    alert = builder(payload)
    if alert is None:
        log_event("DEFI_FEATURE_EVENT_NO_ALERT", details={"event_name": event_name})
        return
    log_event(
        "DEFI_FEATURE_EVENT_DISPATCHED",
        details={
            "event_name": event_name,
            "alert_type": alert.alert_type.value,
            "alert_code": alert.code.value if alert.code is not None else "",
            "severity": alert.severity,
        },
    )
    route_defi_alert(alert)
