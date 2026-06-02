"""Gateway handler wiring recon-age + immediate-SEV0 evaluators to the freeze publisher.

This is the entry point that closes the G12 recon-freeze safety chain
(observability_master G12 P0). It wires together three previously-disconnected
pieces:

  1. ``evaluate_recon_age``       — recon-age band ladder (was unwired)
  2. ``evaluate_immediate_sev0``  — 7 closed-set SEV0 predicates (was unwired)
  3. ``arm_recon_freeze_for_alerts`` — the RECON_FREEZE_ARMED publisher (new)

On a reconciliation-age payload the handler evaluates both rule sets, routes
any CRITICAL alert to PagerDuty + Telegram (mirroring the recon-drift handler
pattern), THEN publishes a ``RECON_FREEZE_ARMED`` coordination event per
CRITICAL recon-age breach + per triggered SEV0 override so the
execution-service ``ReconFreezeChecker`` blocks new orders.

Mirrors the routing pattern in ``recon_drift_event_handler.py``.

Reference: ``plans/epics/observability_master.md`` G12 +
``plans/archive/recon_freeze_armed_never_published_2026_05_27``.
"""

from __future__ import annotations

import logging

from unified_trading_library import log_event

from .notifiers.pagerduty import PagerDutySeverity
from .notifiers.router import route_event_with_explicit_channels
from .recon_freeze_publisher import arm_recon_freeze_for_alerts
from .rules.reconciliation_rules import evaluate_immediate_sev0, evaluate_recon_age

logger = logging.getLogger(__name__)

_PAGING_CHANNELS: frozenset[str] = frozenset({"pagerduty", "telegram"})
_CRITICAL: PagerDutySeverity = "critical"


def _route_critical_alert(alert: dict[str, object]) -> None:
    """Route a single CRITICAL alert to PagerDuty + Telegram."""
    rule_id = str(alert.get("rule_id", ""))  # noqa: qg-empty-fallback
    route_event_with_explicit_channels(
        rule_id,
        dict(alert),
        channels=set(_PAGING_CHANNELS),
        pd_severity=_CRITICAL,
    )


def handle_reconciliation_age_payload(payload: dict[str, object]) -> None:
    """Evaluate a reconciliation-age payload, route alerts, and arm freezes.

    Steps:
      1. Evaluate ``evaluate_recon_age`` + ``evaluate_immediate_sev0``.
      2. Route every CRITICAL alert to PagerDuty + Telegram.
      3. Publish ``RECON_FREEZE_ARMED`` for CRITICAL recon-age + sev0 alerts.

    Args:
        payload: Reconciliation payload with recon-age keys (dimension, venue,
            instrument, strategy_id, unreconciled_age_seconds) AND/OR the
            boolean ImmediateSev0Override predicate keys + context fields.
    """
    log_event(
        "RECONCILIATION_AGE_PAYLOAD_RECEIVED",
        details={
            "dimension": payload.get("dimension"),
            "venue": payload.get("venue"),
            "instrument": payload.get("instrument"),
            "strategy_id": payload.get("strategy_id"),
            "unreconciled_age_seconds": payload.get("unreconciled_age_seconds"),
        },
    )

    recon_age_alerts = evaluate_recon_age(payload)
    sev0_alerts = evaluate_immediate_sev0(payload)

    for alert in recon_age_alerts:
        if str(alert.get("severity", "")) == "CRITICAL":  # noqa: qg-empty-fallback
            _route_critical_alert(alert)
    for alert in sev0_alerts:
        # evaluate_immediate_sev0 emits CRITICAL alerts only, but guard anyway.
        if str(alert.get("severity", "")) == "CRITICAL":  # noqa: qg-empty-fallback
            _route_critical_alert(alert)

    # Arm the recon-freeze coordination events — this is the safety-chain link
    # that blocks new orders in execution-service. Runs AFTER channel dispatch
    # so paging fires even if a downstream publish hiccups.
    published = arm_recon_freeze_for_alerts(recon_age_alerts, sev0_alerts)
    if published:
        logger.warning(
            "Recon-freeze armed: %d freeze event(s) published (%d recon-age CRITICAL, %d sev0)",
            len(published),
            sum(1 for a in recon_age_alerts if str(a.get("severity", "")) == "CRITICAL"),  # noqa: qg-empty-fallback
            len(sev0_alerts),
        )
