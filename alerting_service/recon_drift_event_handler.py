"""Alerting handlers for batch-vs-live reconciliation drift events.

Events consumed:
  BATCH_VS_LIVE_RECON_DRIFTED — batch-vs-live P&L gap exceeds per-archetype bps threshold
      (emitted by batch-live-reconciliation-service orchestrator when
      alpha_pnl_gap_bps > RECON_GREEN_THRESHOLDS[archetype]["bps_delta_max"])

  BATCH_LIVE_RECON_DRIFT — paper-vs-live slippage exceeds 5 bps threshold
      (routing label in source: ALERT_AND_AUTO_DEMOTE)

Both events route CRITICAL → PagerDuty + Telegram.
"""

from __future__ import annotations

import logging

from unified_trading_library import log_event

from .notifiers.pagerduty import PagerDutySeverity
from .notifiers.router import route_event_with_explicit_channels

logger = logging.getLogger(__name__)

_PAGING_CHANNELS: frozenset[str] = frozenset({"pagerduty", "telegram"})
_CRITICAL: PagerDutySeverity = "critical"


def handle_batch_vs_live_recon_drifted_payload(payload: dict[str, object]) -> None:
    """Route BATCH_VS_LIVE_RECON_DRIFTED at CRITICAL severity.

    Payload keys (from orchestrator): date, run_id, archetype,
    alpha_pnl_gap_bps, threshold_bps, routing.
    """
    log_event(
        "BATCH_VS_LIVE_RECON_DRIFTED_RECEIVED",
        details={
            "archetype": payload.get("archetype"),
            "alpha_pnl_gap_bps": payload.get("alpha_pnl_gap_bps"),
            "threshold_bps": payload.get("threshold_bps"),
            "date": payload.get("date"),
        },
    )
    logger.warning(
        "Batch-vs-live recon drift: archetype=%s gap_bps=%.2f threshold_bps=%.2f",
        payload.get("archetype"),
        float(str(payload.get("alpha_pnl_gap_bps", 0))),
        float(str(payload.get("threshold_bps", 0))),
    )
    route_event_with_explicit_channels(
        "BATCH_VS_LIVE_RECON_DRIFTED",
        dict(payload),
        channels=set(_PAGING_CHANNELS),
        pd_severity=_CRITICAL,
    )


def handle_batch_live_recon_drift_payload(payload: dict[str, object]) -> None:
    """Route BATCH_LIVE_RECON_DRIFT at CRITICAL severity.

    Paper-vs-live slippage breach (routing="ALERT_AND_AUTO_DEMOTE").
    Payload keys: date, run_id, slippage_delta_bps, threshold_bps,
    stage3b_deviations, routing.
    """
    log_event(
        "BATCH_LIVE_RECON_DRIFT_RECEIVED",
        details={
            "slippage_delta_bps": payload.get("slippage_delta_bps"),
            "threshold_bps": payload.get("threshold_bps"),
            "stage3b_deviations": payload.get("stage3b_deviations"),
            "date": payload.get("date"),
        },
    )
    logger.warning(
        "Paper-vs-live recon drift: slippage_bps=%.2f threshold_bps=%.2f deviations=%s",
        float(str(payload.get("slippage_delta_bps", 0))),
        float(str(payload.get("threshold_bps", 0))),
        payload.get("stage3b_deviations"),
    )
    route_event_with_explicit_channels(
        "BATCH_LIVE_RECON_DRIFT",
        dict(payload),
        channels=set(_PAGING_CHANNELS),
        pd_severity=_CRITICAL,
    )
