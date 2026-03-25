"""Reconciliation alert evaluation rules.

Evaluates position, balance, and PnL discrepancy events against
thresholds and returns structured alert records for routing to
Telegram (WARNING) or PagerDuty + Telegram (CRITICAL).

Events consumed:
- DEVIATION_CONFIRMED — a reconciliation deviation persisted beyond threshold
- DEVIATION_ESCALATED — a confirmed deviation was escalated to human attention
- BALANCE_DISCREPANCY_DETECTED — balance mismatch on a venue/currency
- UNEXPLAINED_PNL_RESIDUAL — PnL component sum doesn't match exchange
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal


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
            "alert_id": f"recon-pos-{event_details.get('deviation_id', 'unknown')}-{now.strftime('%H%M%S')}",
            "rule_id": "position_qty_discrepancy",
            "metric_name": "position_discrepancy",
            "metric_value": str(event_details.get("discrepancy", "0")),
            "severity": severity,
            "venue": str(event_details.get("venue", "")),
            "instrument": str(event_details.get("instrument", "")),
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

    severity: Literal["WARNING", "CRITICAL"] = "CRITICAL" if status == "CRITICAL" else "WARNING"
    now = datetime.now(UTC)
    return [
        {
            "alert_id": f"recon-bal-{event_details.get('venue', 'unk')}-{event_details.get('currency', 'unk')}-{now.strftime('%H%M%S')}",
            "rule_id": "balance_discrepancy",
            "metric_name": "balance_discrepancy",
            "metric_value": str(event_details.get("discrepancy_total", "0")),
            "severity": severity,
            "venue": str(event_details.get("venue", "")),
            "message": (
                f"Balance discrepancy on {event_details.get('venue', '?')} "
                f"{event_details.get('currency', '?')}: "
                f"diff={event_details.get('discrepancy_total', '?')} "
                f"({event_details.get('discrepancy_pct', '?')})"
            ),
            "created_at": now.isoformat(),
            "delivered": False,
            "delivery_channel": "telegram" if severity == "WARNING" else "pagerduty+telegram",
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
            "alert_id": f"recon-pnl-{event_details.get('venue', 'unk')}-{event_details.get('instrument', 'unk')}-{now.strftime('%H%M%S')}",
            "rule_id": "unexplained_pnl",
            "metric_name": "unexplained_pnl",
            "metric_value": str(event_details.get("unexplained_pnl", "0")),
            "severity": severity,
            "venue": str(event_details.get("venue", "")),
            "instrument": str(event_details.get("instrument", "")),
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
