"""Sports-specific alert rules.

Alert types:
- Arbitrage opportunity detected (odds discrepancy > threshold)
- Steam move detected (sharp line movement)
- Account restriction detected (max stake reduced)
- Venue health degraded (login failures, CAPTCHA rate increase)
- CLV trend negative (model degradation signal)
- Exposure limit approaching
"""

from __future__ import annotations

import logging
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SportsAlertType(StrEnum):
    ARB_OPPORTUNITY = "arb_opportunity"
    STEAM_MOVE = "steam_move"
    ACCOUNT_RESTRICTED = "account_restricted"
    VENUE_HEALTH_DEGRADED = "venue_health_degraded"
    CLV_TREND_NEGATIVE = "clv_trend_negative"
    EXPOSURE_LIMIT_WARNING = "exposure_limit_warning"
    BET_REJECTED = "bet_rejected"
    SETTLEMENT_DELAYED = "settlement_delayed"


class SportsAlert(BaseModel):
    """A sports-specific alert."""
    alert_type: SportsAlertType
    severity: str = "info"
    venue_key: str | None = None
    fixture_id: str | None = None
    message: str
    details: dict[str, str | int | float | bool] = Field(default_factory=dict)


def check_arb_alert(
    edge_pct: Decimal,
    venue_a: str,
    venue_b: str,
    fixture_id: str,
    min_edge_pct: Decimal = Decimal("1.0"),
) -> SportsAlert | None:
    """Generate alert if arb edge exceeds threshold."""
    if edge_pct < min_edge_pct:
        return None
    severity = "critical" if edge_pct > Decimal("3.0") else "warning" if edge_pct > Decimal("2.0") else "info"
    return SportsAlert(
        alert_type=SportsAlertType.ARB_OPPORTUNITY,
        severity=severity,
        fixture_id=fixture_id,
        message=f"Arb {edge_pct}% between {venue_a} and {venue_b}",
        details={"edge_pct": float(edge_pct), "venue_a": venue_a, "venue_b": venue_b},
    )


def check_steam_alert(
    sharp_venue: str,
    movement_pct: Decimal,
    stale_venues: list[str],
) -> SportsAlert | None:
    """Generate alert on steam move detection."""
    if abs(movement_pct) < Decimal("2.0") or not stale_venues:
        return None
    return SportsAlert(
        alert_type=SportsAlertType.STEAM_MOVE,
        severity="warning",
        venue_key=sharp_venue,
        message=f"Steam move {movement_pct}% at {sharp_venue}, {len(stale_venues)} stale venues",
        details={"movement_pct": float(movement_pct), "stale_venues": ",".join(stale_venues)},
    )


def check_account_restriction(
    venue_key: str,
    previous_max_stake: Decimal,
    current_max_stake: Decimal,
) -> SportsAlert | None:
    """Generate alert when a venue reduces max stake."""
    if current_max_stake >= previous_max_stake:
        return None
    reduction_pct = ((previous_max_stake - current_max_stake) / previous_max_stake) * Decimal("100")
    severity = "critical" if reduction_pct > Decimal("50") else "warning"
    return SportsAlert(
        alert_type=SportsAlertType.ACCOUNT_RESTRICTED,
        severity=severity,
        venue_key=venue_key,
        message=f"Max stake reduced {reduction_pct}% at {venue_key}: {previous_max_stake} -> {current_max_stake}",
        details={"reduction_pct": float(reduction_pct), "old_max": float(previous_max_stake), "new_max": float(current_max_stake)},
    )


def check_clv_trend(
    mean_clv_pct: Decimal,
    sample_size: int,
    min_sample: int = 50,
) -> SportsAlert | None:
    """Generate alert if CLV trend turns negative (model degradation)."""
    if sample_size < min_sample:
        return None
    if mean_clv_pct >= Decimal("0"):
        return None
    severity = "critical" if mean_clv_pct < Decimal("-2.0") else "warning"
    return SportsAlert(
        alert_type=SportsAlertType.CLV_TREND_NEGATIVE,
        severity=severity,
        message=f"CLV trend negative: {mean_clv_pct}% over {sample_size} bets — model may be degrading",
        details={"mean_clv_pct": float(mean_clv_pct), "sample_size": sample_size},
    )
