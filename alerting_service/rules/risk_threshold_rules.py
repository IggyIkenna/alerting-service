"""Risk metric threshold evaluation rules.

Evaluates leverage, concentration, and drawdown metrics against
configurable warning/critical thresholds. Returns structured alert
records for metrics that breach thresholds.

This is the canonical threshold evaluator for risk-derived alerts.
Mock mode and production both call these functions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Literal

ThresholdStatus = Literal["OK", "WARNING", "CRITICAL"]

# Default thresholds matching production configuration
DEFAULT_LEVERAGE_WARNING: Final[Decimal] = Decimal("7")
DEFAULT_LEVERAGE_CRITICAL: Final[Decimal] = Decimal("10")
DEFAULT_CONCENTRATION_WARNING: Final[Decimal] = Decimal("0.35")
DEFAULT_CONCENTRATION_CRITICAL: Final[Decimal] = Decimal("0.5")
DEFAULT_DRAWDOWN_WARNING: Final[Decimal] = Decimal("0.10")
DEFAULT_DRAWDOWN_CRITICAL: Final[Decimal] = Decimal("0.15")

_RISK_CHECKS: Final[list[tuple[str, str, Decimal, Decimal]]] = [
    ("leverage", "leverage", DEFAULT_LEVERAGE_WARNING, DEFAULT_LEVERAGE_CRITICAL),
    (
        "concentration",
        "concentration",
        DEFAULT_CONCENTRATION_WARNING,
        DEFAULT_CONCENTRATION_CRITICAL,
    ),
    ("drawdown", "drawdown", DEFAULT_DRAWDOWN_WARNING, DEFAULT_DRAWDOWN_CRITICAL),
]


def get_threshold_status(
    value: Decimal,
    warning_threshold: Decimal,
    critical_threshold: Decimal,
) -> ThresholdStatus:
    """Return OK, WARNING, or CRITICAL based on value vs thresholds."""
    if value >= critical_threshold:
        return "CRITICAL"
    if value >= warning_threshold:
        return "WARNING"
    return "OK"


def evaluate_risk_thresholds(
    risk_metrics: dict[str, object],
) -> list[dict[str, object]]:
    """Evaluate risk metric thresholds and return alert records.

    Args:
        risk_metrics: Dict with keys like 'leverage', 'concentration',
            'drawdown', 'client_id'.

    Returns:
        List of alert dicts for metrics that breach WARNING or CRITICAL.
    """
    alerts: list[dict[str, object]] = []
    now = datetime.now(UTC)

    for metric_name, key, warning, critical in _RISK_CHECKS:
        value = Decimal(str(risk_metrics.get(key, "0")))
        status = get_threshold_status(value, warning, critical)

        if status != "OK":
            alerts.append(
                {
                    "alert_id": f"mock-{metric_name}-{now.strftime('%Y%m%d%H%M%S')}",
                    "metric_name": metric_name,
                    "metric_value": str(value),
                    "threshold_warning": str(warning),
                    "threshold_critical": str(critical),
                    "severity": status,
                    "client_id": str(risk_metrics.get("client_id", "mock-client")),
                    "message": (
                        f"{metric_name} is {status}: {value}"
                        f" (warning={warning}, critical={critical})"
                    ),
                    "created_at": now.isoformat(),
                    "delivered": False,
                    "delivery_channel": "mock-suppressed",
                }
            )

    return alerts
