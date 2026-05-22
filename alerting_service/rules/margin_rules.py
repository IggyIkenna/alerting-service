"""MarginEvent consumer rules (Wave C4 — workspace audit 2026-05-01).

Consumes ``MarginEvent`` published by ``strategy-service`` on
the ``margin-events`` topic. PBM is the single canonical producer; this is the
single canonical alerting consumer. Replaces the prior pattern of each service
re-deriving HF and emitting its own threshold alert.

Severity ladder mapping (UAC ``MarginEventSeverity`` -> alerting event_name):

  INFO         -> MARGIN_INFO            (telegram fallthrough only)
  WARNING      -> MARGIN_WARNING         (telegram)
  CRITICAL     -> MARGIN_CRITICAL        (PagerDuty + Telegram)
  LIQUIDATION  -> MARGIN_LIQUIDATION     (PagerDuty + Telegram)

The router consumes config-driven ``routing_rules`` -- adding ``MARGIN_*`` glob
patterns there opt-in escalates the right severity tiers.

Service event taxonomy:
  SERVICE_EVENT: MARGIN_EVENT_RECEIVED
  SERVICE_EVENT: MARGIN_EVENT_ROUTED
"""

from __future__ import annotations

import json
import logging

from unified_api_contracts import AlertCode
from unified_api_contracts.internal import (  # noqa: qg-deep-import
    MarginEvent,
    MarginEventSeverity,
    MarginHealthSnapshot,
)
from unified_trading_library import log_event

from ..notifiers.router import route_event

logger = logging.getLogger(__name__)


_SEVERITY_TO_EVENT_NAME: dict[MarginEventSeverity, str] = {
    MarginEventSeverity.INFO: AlertCode.MARGIN_INFO.value,
    MarginEventSeverity.WARNING: AlertCode.MARGIN_WARNING.value,
    MarginEventSeverity.CRITICAL: AlertCode.MARGIN_CRITICAL.value,
    MarginEventSeverity.LIQUIDATION: AlertCode.MARGIN_LIQUIDATION.value,
}


def _format_message(event: MarginEvent) -> str:
    """One-line operator-facing summary."""
    snap = event.snapshot
    parts: list[str] = [
        f"venue={snap.venue}",
        f"strategy={snap.strategy_id}",
        f"type={snap.position_type}",
    ]
    if snap.health_factor is not None:
        parts.append(f"HF={snap.health_factor}")
    if snap.margin_usage_pct is not None:
        parts.append(f"margin_used={snap.margin_usage_pct}%")
    if event.distance_to_liquidation_pct is not None:
        parts.append(f"dist_to_liq={event.distance_to_liquidation_pct}%")
    parts.append(f"breached={event.threshold_breached}")
    return " ".join(parts)


def _snapshot_metrics(snap: MarginHealthSnapshot) -> dict[str, object]:
    """Extract optional numeric metrics from the snapshot, dropping unset fields."""
    metrics: dict[str, object] = {}
    if snap.health_factor is not None:
        metrics["health_factor"] = float(snap.health_factor)
    if snap.ltv_ratio is not None:
        metrics["ltv_ratio"] = float(snap.ltv_ratio)
    if snap.margin_usage_pct is not None:
        metrics["margin_usage_pct"] = float(snap.margin_usage_pct)
    if snap.collateral_usd:
        metrics["collateral_usd"] = float(snap.collateral_usd)
    if snap.debt_usd:
        metrics["debt_usd"] = float(snap.debt_usd)
    if snap.liquidation_price is not None:
        metrics["liquidation_price"] = float(snap.liquidation_price)
    return metrics


def _snapshot_identifiers(snap: MarginHealthSnapshot) -> dict[str, object]:
    """Mandatory + optional identifier fields (strategy / client / account)."""
    ids: dict[str, object] = {
        "strategy_id": snap.strategy_id,
        "venue": snap.venue,
        "venue_type": snap.venue_type,
        "position_type": snap.position_type,
    }
    if snap.client_id:
        ids["client_id"] = snap.client_id
    if snap.account_id:
        ids["account_id"] = snap.account_id
    return ids


def _details_payload(event: MarginEvent) -> dict[str, object]:
    """Serialize MarginEvent to the dict shape route_event expects."""
    payload: dict[str, object] = {
        "message": _format_message(event),
        "severity": str(event.margin_severity),
        "source": "alerting-service/margin-rules",
        "event_id": event.event_id,
        "correlation_id": event.correlation_id,
        "threshold_breached": event.threshold_breached,
    }
    payload.update(_snapshot_identifiers(event.snapshot))
    payload.update(_snapshot_metrics(event.snapshot))
    if event.distance_to_liquidation_pct is not None:
        payload["distance_to_liquidation_pct"] = float(event.distance_to_liquidation_pct)
    if event.recommended_action:
        payload["recommended_action"] = event.recommended_action
    return payload


def route_margin_event(event: MarginEvent) -> None:
    """Route a typed MarginEvent through the standard event router.

    Delegates channel selection to the router which reads
    ``AlertingSystemConfig.routing_rules``. Adding ``MARGIN_CRITICAL`` /
    ``MARGIN_LIQUIDATION`` to ``routing_rules`` with PagerDuty channel produces
    a P0 page; ``MARGIN_WARNING`` falls through to Telegram by default.
    """
    event_name = _SEVERITY_TO_EVENT_NAME[event.margin_severity]
    log_event(
        "MARGIN_EVENT_ROUTED",
        details={
            "event_name": event_name,
            "strategy_id": event.snapshot.strategy_id,
            "venue": event.snapshot.venue,
            "venue_type": event.snapshot.venue_type,
        },
    )
    route_event(event_name, _details_payload(event))


def route_margin_event_payload(payload: dict[str, object]) -> None:
    """Deserialize a raw Pub/Sub payload into a MarginEvent and route it.

    Subscriber path: receives JSON bytes -> dict -> here. Validates against
    the UAC schema; on schema mismatch, emits a structured warning and drops
    the message rather than crashing the subscriber loop.
    """
    log_event("MARGIN_EVENT_RECEIVED", details={"event_id": payload.get("event_id")})
    try:
        event = MarginEvent.model_validate(payload)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "Dropping malformed MarginEvent payload: %s | payload=%s",
            exc,
            json.dumps(payload, default=str)[:512],
        )
        return
    route_margin_event(event)
