"""Reconciliation alert evaluation rules.

Evaluates position, balance, and PnL discrepancy events against
thresholds and returns structured alert records for routing to
Telegram (WARNING) or PagerDuty + Telegram (CRITICAL).

Events consumed:
- DEVIATION_CONFIRMED — a reconciliation deviation persisted beyond threshold
- DEVIATION_ESCALATED — a confirmed deviation was escalated to human attention
- BALANCE_DISCREPANCY_DETECTED — balance mismatch on a venue/currency
- UNEXPLAINED_PNL_RESIDUAL — PnL component sum doesn't match exchange
- BATCH_VS_LIVE_RECON_DRIFTED — batch-vs-live P&L gap exceeds archetype threshold
- RECON_AGE_BREACHED — unreconciled age exceeded a threshold band

Age thresholds (per plan Phase 3, P0.7-P0.9):
  warn_seconds=300       (5min)  → WARNING  → Slack
  investigate_seconds=900 (15min) → WARNING  → Slack + SEV1 flag
  critical_seconds=1800   (30min) → CRITICAL → PagerDuty + Telegram (SEV0)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from unified_api_contracts.alerting import AlertSeverity
from unified_api_contracts.incident import ImmediateSev0Override


def evaluate_position_discrepancy(
    event_details: dict[str, object],
) -> list[dict[str, object]]:
    """Evaluate a position discrepancy event and return alert records.

    Args:
        event_details: Event payload with keys: deviation_id, type, venue,
            instrument, discrepancy, discrepancy_pct, elapsed_seconds.

    Returns:
        List of alert dicts (0 or 1 items).
    """
    severity = _severity_from_pct(event_details.get("discrepancy_pct", "0"))
    if severity == "OK":
        return []

    now = datetime.now(UTC)
    return [
        {
            "alert_id": (f"recon-pos-{event_details.get('deviation_id', 'unknown')}-{now.strftime('%H%M%S')}"),
            "rule_id": "position_qty_discrepancy",
            "metric_name": "position_discrepancy",
            "metric_value": str(event_details.get("discrepancy", "0")),
            "severity": severity,
            "venue": str(event_details.get("venue", "")),  # noqa: qg-empty-fallback
            "instrument": str(event_details.get("instrument", "")),  # noqa: qg-empty-fallback
            "message": (
                f"Position discrepancy on {event_details.get('venue', '?')}:"
                f"{event_details.get('instrument', '?')}: "
                f"diff={event_details.get('discrepancy', '?')}"
            ),
            "created_at": now.isoformat(),
            "delivered": False,
            "delivery_channel": "telegram" if severity == "WARNING" else "pagerduty+telegram",
        }
    ]


def evaluate_balance_discrepancy(
    event_details: dict[str, object],
) -> list[dict[str, object]]:
    """Evaluate a balance discrepancy event.

    Args:
        event_details: Event payload with keys: venue, currency, status,
            discrepancy_total, discrepancy_pct.
    """
    status = str(event_details.get("status", "match")).upper()
    if status == "MATCH":
        return []

    severity: AlertSeverity = AlertSeverity.CRITICAL if status == "CRITICAL" else AlertSeverity.WARN
    now = datetime.now(UTC)
    return [
        {
            "alert_id": (
                f"recon-bal-{event_details.get('venue', 'unk')}"
                f"-{event_details.get('currency', 'unk')}"
                f"-{now.strftime('%H%M%S')}"
            ),
            "rule_id": "balance_discrepancy",
            "metric_name": "balance_discrepancy",
            "metric_value": str(event_details.get("discrepancy_total", "0")),
            "severity": severity,
            "venue": str(event_details.get("venue", "")),  # noqa: qg-empty-fallback
            "message": (
                f"Balance discrepancy on {event_details.get('venue', '?')} "
                f"{event_details.get('currency', '?')}: "
                f"diff={event_details.get('discrepancy_total', '?')} "
                f"({event_details.get('discrepancy_pct', '?')})"
            ),
            "created_at": now.isoformat(),
            "delivered": False,
            "delivery_channel": "telegram" if severity is AlertSeverity.WARN else "pagerduty+telegram",
        }
    ]


def evaluate_pnl_discrepancy(
    event_details: dict[str, object],
) -> list[dict[str, object]]:
    """Evaluate an unexplained PnL residual event.

    Args:
        event_details: Event payload with keys: venue, instrument,
            unexplained_pnl, unexplained_pct.
    """
    severity = _severity_from_pct(event_details.get("unexplained_pct", "0"))
    if severity == "OK":
        return []

    now = datetime.now(UTC)
    return [
        {
            "alert_id": (
                f"recon-pnl-{event_details.get('venue', 'unk')}"
                f"-{event_details.get('instrument', 'unk')}"
                f"-{now.strftime('%H%M%S')}"
            ),
            "rule_id": "unexplained_pnl",
            "metric_name": "unexplained_pnl",
            "metric_value": str(event_details.get("unexplained_pnl", "0")),
            "severity": severity,
            "venue": str(event_details.get("venue", "")),  # noqa: qg-empty-fallback
            "instrument": str(event_details.get("instrument", "")),  # noqa: qg-empty-fallback
            "message": (
                f"Unexplained PnL on {event_details.get('venue', '?')}:"
                f"{event_details.get('instrument', '?')}: "
                f"residual={event_details.get('unexplained_pnl', '?')} "
                f"({event_details.get('unexplained_pct', '?')})"
            ),
            "created_at": now.isoformat(),
            "delivered": False,
            "delivery_channel": "telegram" if severity == "WARNING" else "pagerduty+telegram",
        }
    ]


def evaluate_batch_vs_live_recon_drifted(
    event_details: dict[str, object],
) -> list[dict[str, object]]:
    """Evaluate a BATCH_VS_LIVE_RECON_DRIFTED event from batch-live-reconciliation-service.

    Args:
        event_details: Event payload with keys: date, run_id, archetype,
            alpha_pnl_gap_bps, threshold_bps, routing.
    """
    alpha_pnl_gap_bps = float(str(event_details.get("alpha_pnl_gap_bps", "0")))
    threshold_bps = float(str(event_details.get("threshold_bps", "50")))
    archetype = str(event_details.get("archetype", "unknown"))  # noqa: qg-empty-fallback
    date = str(event_details.get("date", ""))  # noqa: qg-empty-fallback

    # P&L gap >2x threshold is CRITICAL; between 1x-2x is WARN
    severity: AlertSeverity = AlertSeverity.CRITICAL if alpha_pnl_gap_bps > threshold_bps * 2 else AlertSeverity.WARN
    now = datetime.now(UTC)
    return [
        {
            "alert_id": f"batch-live-drift-{archetype}-{date}-{now.strftime('%H%M%S')}",
            "rule_id": "batch_vs_live_recon_drifted",
            "metric_name": "alpha_pnl_gap_bps",
            "metric_value": f"{alpha_pnl_gap_bps:.1f}",
            "severity": severity,
            "archetype": archetype,
            "message": (
                f"Batch-vs-live P&L gap {alpha_pnl_gap_bps:.1f} bps "
                f"exceeds threshold {threshold_bps:.0f} bps for {archetype} on {date}"
            ),
            "created_at": now.isoformat(),
            "delivered": False,
            "delivery_channel": "telegram" if severity is AlertSeverity.WARN else "pagerduty+telegram",
        }
    ]


def _severity_from_pct(
    pct_str: object,
    warning_threshold: Decimal = Decimal("0.01"),
    critical_threshold: Decimal = Decimal("0.05"),
) -> Literal["OK", "WARNING", "CRITICAL"]:
    """Convert a percentage string to severity level."""
    try:
        # Handle both "5.00%" format and "0.05" format
        s = str(pct_str).rstrip("%")
        pct = Decimal(s)
        # If > 1, assume it's already a percentage (e.g. "5.00")
        if pct > 1:
            pct = pct / Decimal("100")
    except (ValueError, ArithmeticError):
        return "OK"

    if pct >= critical_threshold:
        return "CRITICAL"
    if pct >= warning_threshold:
        return "WARNING"
    return "OK"


def evaluate_recon_age(
    event_details: dict[str, object],
    *,
    warn_seconds: int = 300,
    investigate_seconds: int = 900,
    critical_seconds: int = 1800,
) -> list[dict[str, object]]:
    """Evaluate unreconciled age and return escalation alerts.

    Three-band ladder (P0.7-P0.9):
      [0, warn)         → no alert
      [warn, investigate) → WARNING  (Slack)
      [investigate, critical) → WARNING + sev1_escalate flag (Slack + SEV1)
      [critical, ∞)     → CRITICAL  (PagerDuty + Telegram = SEV0)

    Per-venue / per-strategy overrides are passed via warn_seconds /
    investigate_seconds / critical_seconds kwargs — callers resolve from
    UAC per_archetype_overrides before calling.

    Args:
        event_details: Payload with keys: dimension, venue, instrument,
            strategy_id, unreconciled_age_seconds, client_id (optional).
        warn_seconds: Age threshold for WARNING band.
        investigate_seconds: Age threshold for SEV1-flag band.
        critical_seconds: Age threshold for CRITICAL/SEV0 band.

    Returns:
        List of alert dicts (0 or 1 items).
    """
    try:
        age = int(str(event_details.get("unreconciled_age_seconds", "0")))
    except (ValueError, TypeError):
        return []

    if age < warn_seconds:
        return []

    now = datetime.now(UTC)
    venue = str(event_details.get("venue", ""))  # noqa: qg-empty-fallback
    instrument = str(event_details.get("instrument", ""))  # noqa: qg-empty-fallback
    strategy_id = str(event_details.get("strategy_id", ""))  # noqa: qg-empty-fallback
    dimension = str(event_details.get("dimension", "UNKNOWN"))  # noqa: qg-empty-fallback

    if age >= critical_seconds:
        severity: AlertSeverity = AlertSeverity.CRITICAL
        rule_id = "RECONCILIATION_AGE_CRITICAL"
        delivery_channel = "pagerduty+telegram"
        threshold = critical_seconds
    elif age >= investigate_seconds:
        severity = AlertSeverity.WARN
        rule_id = "RECONCILIATION_AGE_INVESTIGATE"
        delivery_channel = "telegram"
        threshold = investigate_seconds
    else:
        severity = AlertSeverity.WARN
        rule_id = "RECONCILIATION_AGE_WARN"
        delivery_channel = "telegram"
        threshold = warn_seconds

    return [
        {
            "alert_id": f"recon-age-{venue}-{instrument}-{now.strftime('%H%M%S')}",
            "rule_id": rule_id,
            "metric_name": "unreconciled_age_seconds",
            "metric_value": str(age),
            "severity": severity,
            "venue": venue,
            "instrument": instrument,
            "strategy_id": strategy_id,
            "dimension": dimension,
            "message": (
                f"Recon age breach on {dimension} {venue}:{instrument} "
                f"(strategy={strategy_id}): {age}s unreconciled "
                f"(threshold={threshold}s)"
            ),
            "created_at": now.isoformat(),
            "delivered": False,
            "delivery_channel": delivery_channel,
            "sev1_escalate": age >= investigate_seconds and age < critical_seconds,
        }
    ]


# Mapping from ImmediateSev0Override member → event_details key
_SEV0_PREDICATE_KEYS: dict[ImmediateSev0Override, str] = {
    ImmediateSev0Override.UNKNOWN_NET_EXPOSURE: "unknown_net_exposure",
    ImmediateSev0Override.OPEN_ORDERS_UNCONFIRMABLE: "open_orders_unconfirmable",
    ImmediateSev0Override.KILL_SWITCH_CANNOT_CONFIRM_CANCEL: "kill_switch_cannot_confirm_cancel",
    ImmediateSev0Override.VENUE_INTERNAL_BALANCE_MISMATCH: "venue_internal_balance_mismatch",
    ImmediateSev0Override.POSITION_EXISTS_EXTERNALLY_UNKNOWN_INTERNALLY: (
        "position_exists_externally_unknown_internally"
    ),
    ImmediateSev0Override.MATERIAL_BALANCE_MOVEMENT_UNEXPLAINED: ("material_balance_movement_unexplained"),
    ImmediateSev0Override.MARGIN_COLLATERAL_SAFETY_UNCERTAIN: "margin_collateral_safety_uncertain",
}


def evaluate_immediate_sev0(
    row: dict[str, object],
) -> list[dict[str, object]]:
    """Evaluate 7 closed-set ImmediateSev0Override predicates.

    Returns one alert per triggered predicate (severity=CRITICAL,
    delivery=pagerduty+telegram). Called BEFORE severity-hint routing
    in the alerting gateway — any True predicate forces SEV0.

    Args:
        row: Payload with boolean keys for each predicate (snake_case of
             ImmediateSev0Override members) plus optional venue/instrument/
             strategy_id/client_id context fields.

    Returns:
        List of CRITICAL alert dicts, one per triggered predicate.
    """
    now = datetime.now(UTC)
    venue = str(row.get("venue", ""))  # noqa: qg-empty-fallback
    instrument = str(row.get("instrument", ""))  # noqa: qg-empty-fallback
    strategy_id = str(row.get("strategy_id", ""))  # noqa: qg-empty-fallback
    # account_id / client_id are carried through so account-level SEV0 overrides can
    # arm an ACCOUNT-WIDE recon-freeze (recon_freeze_publisher) with account context.
    account_id = str(row.get("account_id", ""))  # noqa: qg-empty-fallback
    client_id = str(row.get("client_id", ""))  # noqa: qg-empty-fallback

    alerts: list[dict[str, object]] = []
    for override, key in _SEV0_PREDICATE_KEYS.items():
        if row.get(key):
            alerts.append(
                {
                    "alert_id": f"sev0-{override.value.lower()}-{venue}-{now.strftime('%H%M%S')}",
                    "rule_id": f"IMMEDIATE_SEV0_{override.value}",
                    "metric_name": key,
                    "metric_value": "True",
                    "severity": "CRITICAL",
                    "override": override.value,
                    "venue": venue,
                    "instrument": instrument,
                    "strategy_id": strategy_id,
                    "account_id": account_id,
                    "client_id": client_id,
                    "message": (
                        f"ImmediateSev0Override triggered: {override.value} "
                        f"on {venue}:{instrument} (strategy={strategy_id})"
                    ),
                    "created_at": now.isoformat(),
                    "delivered": False,
                    "delivery_channel": "pagerduty+telegram",
                    "sev1_escalate": False,
                }
            )
    return alerts
