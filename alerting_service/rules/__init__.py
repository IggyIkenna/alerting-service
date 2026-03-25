"""Alert routing rules for specific event categories."""

from unified_api_contracts import DefiAlertType
from unified_internal_contracts import DefiAlert

from .data_freshness_rules import route_data_freshness_event
from .defi_rules import (
    check_aave_utilization,
    check_feature_staleness,
    check_funding_rate_flip,
    check_health_factor,
    check_weeth_depeg,
    route_defi_alert,
)
from .reconciliation_rules import (
    evaluate_balance_discrepancy,
    evaluate_pnl_discrepancy,
    evaluate_position_discrepancy,
)
from .sports_rules import (
    SportsAlert,
    SportsAlertType,
    check_account_restriction,
    check_arb_alert,
    check_clv_trend,
    check_steam_alert,
)

__all__ = [
    "DefiAlert",
    "DefiAlertType",
    "SportsAlert",
    "SportsAlertType",
    "check_aave_utilization",
    "check_account_restriction",
    "check_arb_alert",
    "check_clv_trend",
    "check_feature_staleness",
    "check_funding_rate_flip",
    "check_health_factor",
    "check_steam_alert",
    "check_weeth_depeg",
    "evaluate_balance_discrepancy",
    "evaluate_pnl_discrepancy",
    "evaluate_position_discrepancy",
    "route_data_freshness_event",
    "route_defi_alert",
]
