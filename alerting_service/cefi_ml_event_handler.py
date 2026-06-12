"""Consumer for CeFi ML lifecycle events — signal-staleness 3-tier logic.

Bridges ml-inference-service / strategy-service ML events to the alerting
routing layer. The key added value over the generic ``route_event`` path is the
**3-tier ML_SIGNAL_STALENESS ladder**:

  age < 4h   → no alert (within SLA)
  4h ≤ age < 12h  → WARN  → Telegram + Slack   (``ML_SIGNAL_STALENESS``)
  12h ≤ age < 24h → CRITICAL → PagerDuty + Telegram (escalated channels)
  age ≥ 24h       → KILL_SWITCH → ``KILL_SWITCH_ML_MODEL_FAILURE`` + page

All other CeFi-ML event names (``ML_PNL_DEVIATION``, ``ORDER_REJECTION_SPIKE``,
``POSITION_CRITICAL_DISCREPANCY``) already route correctly through the generic
``route_event`` path via their ``LIVE_ALERT_RULES`` entries; they are listed in
``CEFI_ML_PASSTHROUGH_EVENT_NAMES`` for subscriber-dispatch completeness but do
not require special handler logic here.

Threshold ladder (locked design — ``cefi_ml_directional_continuous_live``
2026-06-20, § "Locked design 2026-05-08"):
  ML_SIGNAL_STALENESS_WARN_HOURS    = 4
  ML_SIGNAL_STALENESS_CRITICAL_HOURS = 12
  ML_SIGNAL_STALENESS_KILL_HOURS    = 24

Service event taxonomy:
  SERVICE_EVENT: CEFI_ML_EVENT_RECEIVED
  SERVICE_EVENT: CEFI_ML_EVENT_DISPATCHED
  SERVICE_EVENT: CEFI_ML_EVENT_NO_ALERT
  SERVICE_EVENT: CEFI_ML_EVENT_INVALID
"""

from __future__ import annotations

import logging
from typing import Final

from unified_api_contracts import AlertCode
from unified_trading_library import log_event

from .notifiers.router import route_event, route_event_with_explicit_channels

logger = logging.getLogger(__name__)

# Canonical ML event names emitted by ml-inference-service / strategy-service.
ML_SIGNAL_STALENESS_EVENT: Final[str] = "ML_SIGNAL_STALENESS"

# Events that route through the generic ``route_event`` path unchanged.
# Listed here so ``dispatch_event`` can route them without a dedicated handler.
CEFI_ML_PASSTHROUGH_EVENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "ML_PNL_DEVIATION",
        "ML_MODEL_DRIFT_DETECTED",
        "ML_INFERENCE_LATENCY_BREACH",
        "ML_MODEL_VERSION_MISMATCH",
        "ORDER_REJECTION_SPIKE",
        "POSITION_CRITICAL_DISCREPANCY",
        "POSITION_DRIFT_DETECTED",
    }
)

# All CeFi-ML event names this module handles (dedicated + passthrough).
CEFI_ML_EVENT_NAMES: Final[frozenset[str]] = frozenset({ML_SIGNAL_STALENESS_EVENT} | CEFI_ML_PASSTHROUGH_EVENT_NAMES)

# 3-tier ML_SIGNAL_STALENESS thresholds (locked, cefi_ml_directional_continuous_live).
_WARN_THRESHOLD_SECONDS: Final[float] = 4.0 * 3600.0  # 4h
_CRITICAL_THRESHOLD_SECONDS: Final[float] = 12.0 * 3600.0  # 12h
_KILL_THRESHOLD_SECONDS: Final[float] = 24.0 * 3600.0  # 24h

_PAGING_CHANNELS: Final[set[str]] = {"pagerduty", "telegram"}


def _coerce_float(value: object, field: str) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (ValueError, TypeError):
        logger.warning("CEFI_ML_EVENT_INVALID: field=%s value=%r", field, value)
        return None


def _handle_ml_signal_staleness(payload: dict[str, object]) -> None:
    """Apply the 3-tier staleness ladder for ML_SIGNAL_STALENESS.

    Payload contract:
      ``age_seconds``: float — current age of the last emitted ML signal.
      ``archetype``: str  — archetype identifier (e.g. "ML_DIRECTIONAL_CONTINUOUS").
      ``venue``: str      — venue scope (e.g. "OKX", "BINANCE", "BYBIT").
      ``model_version``: str? — model artefact version for traceability.
    """
    age_seconds = _coerce_float(payload.get("age_seconds"), "age_seconds")
    if age_seconds is None:
        log_event("CEFI_ML_EVENT_INVALID", details={"reason": "missing age_seconds"})
        return

    archetype = str(payload.get("archetype", "ML_DIRECTIONAL_CONTINUOUS"))
    venue = str(payload.get("venue", "unknown"))
    model_version = str(payload["model_version"]) if payload.get("model_version") is not None else None
    age_hours = age_seconds / 3600.0

    base_details: dict[str, object] = {
        "age_seconds": age_seconds,
        "age_hours": round(age_hours, 2),
        "archetype": archetype,
        "venue": venue,
        "source": "alerting-service/cefi-ml",
    }
    if model_version:
        base_details["model_version"] = model_version

    if age_seconds < _WARN_THRESHOLD_SECONDS:
        log_event("CEFI_ML_EVENT_NO_ALERT", details={"event_name": ML_SIGNAL_STALENESS_EVENT, "age_hours": age_hours})
        return

    if age_seconds >= _KILL_THRESHOLD_SECONDS:
        # 24h+ → arm kill-switch for this archetype.
        details: dict[str, object] = {
            **base_details,
            "message": (
                f"ML signal staleness KILL-SWITCH: {archetype} on {venue} "
                f"age={age_hours:.1f}h (≥24h threshold) — halting archetype"
            ),
            "severity": "critical",
        }
        log_event(
            "CEFI_ML_EVENT_DISPATCHED",
            details={"event_name": AlertCode.KILL_SWITCH_ML_MODEL_FAILURE.value, "age_hours": age_hours},
        )
        route_event(AlertCode.KILL_SWITCH_ML_MODEL_FAILURE.value, details)
        return

    if age_seconds >= _CRITICAL_THRESHOLD_SECONDS:
        # 12h-24h → CRITICAL, page on-call.
        details = {
            **base_details,
            "message": (
                f"ML signal staleness CRITICAL: {archetype} on {venue} "
                f"age={age_hours:.1f}h (≥12h, <24h) — investigate model server"
            ),
            "severity": "critical",
        }
        log_event(
            "CEFI_ML_EVENT_DISPATCHED",
            details={"event_name": ML_SIGNAL_STALENESS_EVENT, "tier": "critical", "age_hours": age_hours},
        )
        route_event_with_explicit_channels(
            ML_SIGNAL_STALENESS_EVENT,
            details,
            channels=_PAGING_CHANNELS,
            pd_severity="critical",
        )
        return

    # 4h-12h → WARN, notify channel (Telegram + Slack per LIVE_ALERT_RULES).
    details = {
        **base_details,
        "message": (
            f"ML signal staleness WARNING: {archetype} on {venue} "
            f"age={age_hours:.1f}h (≥4h, <12h) — signal refresh delayed"
        ),
        "severity": "warning",
    }
    log_event(
        "CEFI_ML_EVENT_DISPATCHED",
        details={"event_name": ML_SIGNAL_STALENESS_EVENT, "tier": "warn", "age_hours": age_hours},
    )
    route_event(ML_SIGNAL_STALENESS_EVENT, details)


def handle_cefi_ml_event(event_name: str, payload: dict[str, object]) -> None:
    """Dispatch a CeFi ML event to the matching handler.

    ``ML_SIGNAL_STALENESS`` events are dispatched to the 3-tier staleness
    ladder. All other CeFi-ML event names in ``CEFI_ML_PASSTHROUGH_EVENT_NAMES``
    are forwarded to the generic ``route_event`` path.

    Unknown event names are dropped with a debug log — this handler is only
    called for names in ``CEFI_ML_EVENT_NAMES``.
    """
    log_event("CEFI_ML_EVENT_RECEIVED", details={"event_name": event_name})

    if event_name == ML_SIGNAL_STALENESS_EVENT:
        _handle_ml_signal_staleness(payload)
        return

    if event_name in CEFI_ML_PASSTHROUGH_EVENT_NAMES:
        route_event(event_name, payload)
        return

    logger.debug("CEFI_ML_EVENT_RECEIVED: unrecognised event_name=%s", event_name)
