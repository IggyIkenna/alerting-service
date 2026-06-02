"""Risk metric threshold evaluation rules.

Evaluates leverage, concentration, and drawdown metrics against
configurable warning/critical thresholds. Returns structured alert
records for metrics that breach thresholds.

This is the canonical threshold evaluator for risk-derived alerts.
Mock mode and production both call these functions.

Per-mode threshold behaviour (pvl-p22a):
  LIVE      — default thresholds; page on-call on CRITICAL.
  MANUAL    — same thresholds; alerts carry wake_operator=True (notify operator
              dashboard instead of paging on-call).
  PAPER     — 1.5x looser thresholds; paper runs explore riskier positions.
  BACKTEST  — suppress all alerts; backtests cover extreme scenarios by design.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Literal

from unified_api_contracts import AlertCode
from unified_api_contracts.internal.modes import OperationalMode

ThresholdStatus = Literal["OK", "WARNING", "CRITICAL"]

# Default thresholds matching production configuration
DEFAULT_LEVERAGE_WARNING: Final[Decimal] = Decimal("7")
DEFAULT_LEVERAGE_CRITICAL: Final[Decimal] = Decimal("10")
DEFAULT_CONCENTRATION_WARNING: Final[Decimal] = Decimal("0.35")
DEFAULT_CONCENTRATION_CRITICAL: Final[Decimal] = Decimal("0.5")
DEFAULT_DRAWDOWN_WARNING: Final[Decimal] = Decimal("0.10")
DEFAULT_DRAWDOWN_CRITICAL: Final[Decimal] = Decimal("0.15")

# Paper mode allows 50% slack on top of live thresholds
_PAPER_MULTIPLIER: Final[Decimal] = Decimal(3) / Decimal(2)

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
    mode: OperationalMode = OperationalMode.LIVE,
) -> list[dict[str, object]]:
    """Evaluate risk metric thresholds and return alert records.

    Args:
        risk_metrics: Dict with keys like 'leverage', 'concentration',
            'drawdown', 'client_id'.
        mode: Operational mode from the instruction envelope (pvl-p22a).
            BACKTEST suppresses all alerts; PAPER applies looser thresholds;
            MANUAL uses live thresholds but marks alerts for operator wake-up.

    Returns:
        List of alert dicts for metrics that breach WARNING or CRITICAL.
        Empty for BACKTEST mode.
    """
    if mode == OperationalMode.BACKTEST:
        return []

    multiplier = _PAPER_MULTIPLIER if mode == OperationalMode.PAPER else Decimal("1")
    wake_operator = mode == OperationalMode.MANUAL

    alerts: list[dict[str, object]] = []
    now = datetime.now(UTC)

    for metric_name, key, warning_base, critical_base in _RISK_CHECKS:
        warning = warning_base * multiplier
        critical = critical_base * multiplier
        value = Decimal(str(risk_metrics.get(key, "0")))
        status = get_threshold_status(value, warning, critical)

        if status != "OK":
            alert_code = (
                AlertCode.RISK_RULE_BLOCKED.value if status == "CRITICAL" else AlertCode.RISK_RULE_MONITOR_FIRED.value
            )
            alerts.append(
                {
                    "alert_id": f"mock-{metric_name}-{now.strftime('%Y%m%d%H%M%S')}",
                    "metric_name": metric_name,
                    "metric_value": str(value),
                    "threshold_warning": str(warning),
                    "threshold_critical": str(critical),
                    "severity": status,
                    "alert_code": alert_code,
                    "mode": mode.value,
                    "wake_operator": wake_operator,
                    "client_id": str(risk_metrics.get("client_id", "mock-client")),
                    "message": (
                        f"{metric_name} is {status}: {value}"
                        f" (warning={warning}, critical={critical}, mode={mode.value})"
                    ),
                    "created_at": now.isoformat(),
                    "delivered": False,
                    "delivery_channel": "mock-suppressed",
                }
            )

    return alerts
