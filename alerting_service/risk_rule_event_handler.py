"""Consumer for ``RiskRuleFiredEvent`` (risk_simulations_limits_alerting Phase 5.B).

risk-and-exposure-service emits a typed ``unified_api_contracts.risk.RiskRuleFiredEvent``
on every rule fire (Phase 5.A — emit side, owned by that service). This module is
the alerting-service consumer: it maps ``event.alert_code`` → the matching
``LIVE_ALERT_RULES`` entry (already seeded in UAC with the 4 ``RISK_RULE_*``
codes) and routes the alert through the standard event router at the rule's
configured channels + severity.

Severity → channel mapping (UAC ``LIVE_ALERT_RULES`` SSOT, per § 7 seam diagram in
``risk_simulations_limits_alerting_2026_05_10.md``):

  BLOCK      → RISK_RULE_BLOCKED        HIGH    → PagerDuty + Telegram (page)
  SCALE_DOWN → RISK_RULE_SCALED_DOWN    WARN    → Telegram (dashboard)
  MONITOR    → RISK_RULE_MONITOR_FIRED  INFO    → LogOnly
  TEST_ONLY  → RISK_RULE_TEST_ONLY_ROUTED INFO  → LogOnly

The router (`notifiers/router.py`) reads channel routing from
``AlertingSystemConfig.routing_rules`` which is a byte-for-byte render of
``LIVE_ALERT_RULES``; passing ``event.alert_code.value`` as the router
``event_name`` therefore resolves to the correct tier without a parallel SSOT.

Service event taxonomy:
  SERVICE_EVENT: RISK_RULE_EVENT_RECEIVED
  SERVICE_EVENT: RISK_RULE_EVENT_ROUTED
"""

from __future__ import annotations

import json
import logging

from unified_api_contracts.risk import RiskRuleFiredEvent
from unified_trading_library import log_event

from .notifiers.router import route_event

logger = logging.getLogger(__name__)


def _format_message(event: RiskRuleFiredEvent) -> str:
    """One-line operator-facing summary of a fired risk rule."""
    parts: list[str] = [
        f"rule={event.rule_id.value}",
        f"consequence={event.consequence.value}",
        f"scope={event.scope.value}",
        f"applies_to={event.applies_to}",
    ]
    if event.instruction_id:
        parts.append(f"instruction={event.instruction_id}")
    if event.triggers_kill_switch:
        ks_scope = event.kill_switch_scope.value if event.kill_switch_scope is not None else "?"
        parts.append(f"triggers_kill_switch=True(scope={ks_scope})")
    for key, val in sorted(event.trigger_detail.items()):
        parts.append(f"{key}={val}")
    return " ".join(parts)


def _details_payload(event: RiskRuleFiredEvent) -> dict[str, object]:
    """Serialize a ``RiskRuleFiredEvent`` to the dict shape ``route_event`` expects.

    ``trigger_detail`` + ``metadata`` are rendered into the body. The router
    reads channels/severity from the routing rules keyed on the event name
    (= ``event.alert_code.value``); we still carry ``severity`` for observability.
    """
    payload: dict[str, object] = {
        "message": _format_message(event),
        "severity": event.alerting_severity.value,
        "source": "alerting-service/risk-rules",
        "alert_code": event.alert_code.value,
        "rule_id": event.rule_id.value,
        "scope": event.scope.value,
        "applies_to": event.applies_to,
        "consequence": event.consequence.value,
        "fired_at": event.fired_at.isoformat(),
        "triggers_kill_switch": event.triggers_kill_switch,
    }
    if event.instruction_id:
        payload["instruction_id"] = event.instruction_id
    if event.kill_switch_scope is not None:
        payload["kill_switch_scope"] = event.kill_switch_scope.value
    for key, val in event.trigger_detail.items():
        payload[f"trigger_{key}"] = val
    for key, val in event.metadata.items():
        payload[f"meta_{key}"] = val
    return payload


def handle_risk_rule_fired_event(event: RiskRuleFiredEvent) -> None:
    """Route a typed ``RiskRuleFiredEvent`` through the standard event router.

    Channel selection is delegated to ``route_event`` which matches
    ``event.alert_code.value`` against the UAC-seeded routing rules:
    HIGH → PagerDuty+Telegram, WARN → Telegram, INFO → LogOnly.
    """
    event_name = event.alert_code.value
    log_event(
        "RISK_RULE_EVENT_ROUTED",
        details={
            "event_name": event_name,
            "rule_id": event.rule_id.value,
            "consequence": event.consequence.value,
            "scope": event.scope.value,
            "severity": event.alerting_severity.value,
        },
    )
    route_event(event_name, _details_payload(event))


def handle_risk_rule_fired_payload(payload: dict[str, object]) -> None:
    """Deserialize a raw Pub/Sub payload into a ``RiskRuleFiredEvent`` and route it.

    Subscriber path: JSON bytes → dict → here. On schema mismatch, emit a
    structured warning and drop the message rather than crashing the loop.
    """
    log_event("RISK_RULE_EVENT_RECEIVED", details={"alert_code": payload.get("alert_code")})
    try:
        event = RiskRuleFiredEvent.model_validate(payload)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "Dropping malformed RiskRuleFiredEvent payload: %s | payload=%s",
            exc,
            json.dumps(payload, default=str)[:512],
        )
        return
    handle_risk_rule_fired_event(event)
