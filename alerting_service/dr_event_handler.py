"""Consumer for disaster-recovery typed events (disaster_recovery_circuit_breakers Phase 4.C).

Three event families, all from the DR plan's UAC contracts:

1. ``KillSwitchArmedEvent`` — a kill switch was armed (operator-manual or
   breaker-auto). Severity tier is a function of ``KillSwitchId``: a global /
   per-wallet / per-asset-group arm pages at CRITICAL; a per-venue / per-archetype
   arm pages at HIGH. Both go to PagerDuty + Telegram.
2. ``KillSwitchDisarmEvent`` — a kill switch was disarmed. The two RECOVERY
   AlertCodes (``KILL_SWITCH_AUTO_RECOVERED`` for ``AUTO_COOLDOWN`` recovery,
   ``KILL_SWITCH_MANUAL_UNKILLED`` for ``MANUAL_UNKILL``) are INFO → Telegram
   (already seeded in ``LIVE_ALERT_RULES``), so we route through the standard
   ``route_event`` keyed on the AlertCode value.
3. Circuit-breaker fire events — no typed ``BreakerFiredEvent`` UAC model exists
   yet (FLAGGED to risk/DR plan owners). For now we classify off the
   ``CircuitBreakerId`` + the ``BreakerAction`` of its registry config:
   ``KILL_ALL`` → CRITICAL + page; ``CANCEL_OPEN`` → HIGH + page;
   ``SCALE_DOWN`` / ``BLOCK_NEW`` → WARN → Telegram.

Service event taxonomy:
  SERVICE_EVENT: KILL_SWITCH_ARM_RECEIVED
  SERVICE_EVENT: KILL_SWITCH_DISARM_RECEIVED
  SERVICE_EVENT: CIRCUIT_BREAKER_FIRE_RECEIVED
  SERVICE_EVENT: DR_EVENT_ROUTED
"""

from __future__ import annotations

import json
import logging

from unified_api_contracts import (
    AlertSeverity,
    BreakerAction,
    BreakerRecoveryMode,
    CircuitBreakerId,
    KillSwitchArmedEvent,
    KillSwitchDisarmEvent,
    KillSwitchId,
)
from unified_api_contracts.alerting import AlertCode
from unified_api_contracts.registry.circuit_breakers import (  # noqa: qg-deep-import
    PER_ARCHETYPE_BREAKERS,
)
from unified_trading_library import log_event

from .notifiers.pagerduty import PagerDutySeverity
from .notifiers.router import route_event, route_event_with_explicit_channels

logger = logging.getLogger(__name__)

# ── Kill-switch arm severity tiering ─────────────────────────────────────────
# A global / per-wallet / per-asset-group arm is a portfolio-wide stop → CRITICAL.
# A per-venue / per-archetype arm is a scoped stop → HIGH. Both page.
_CRITICAL_ARM_SWITCH_IDS: frozenset[KillSwitchId] = frozenset(
    {
        KillSwitchId.KILL_ALL_LIVE,
        KillSwitchId.KILL_PER_WALLET,
        KillSwitchId.KILL_PER_ASSET_GROUP_CEFI,
        KillSwitchId.KILL_PER_ASSET_GROUP_DEFI,
    }
)

_PAGING_CHANNELS: frozenset[str] = frozenset({"pagerduty", "telegram"})

# ── Circuit-breaker action → severity tier ───────────────────────────────────
_BREAKER_ACTION_SEVERITY: dict[BreakerAction, AlertSeverity] = {
    BreakerAction.KILL_ALL: AlertSeverity.CRITICAL,
    BreakerAction.CANCEL_OPEN: AlertSeverity.HIGH,
    BreakerAction.SCALE_DOWN: AlertSeverity.WARN,
    BreakerAction.BLOCK_NEW: AlertSeverity.WARN,
}

# ── Recovery mode → RECOVERY AlertCode (DR plan Phase 5.A) ───────────────────
_RECOVERY_MODE_ALERT_CODE: dict[BreakerRecoveryMode, AlertCode] = {
    BreakerRecoveryMode.AUTO_COOLDOWN: AlertCode.KILL_SWITCH_AUTO_RECOVERED,
    BreakerRecoveryMode.MANUAL_UNKILL: AlertCode.KILL_SWITCH_MANUAL_UNKILLED,
}


def _breaker_action_for(breaker_id: CircuitBreakerId) -> BreakerAction | None:
    """Look up the ``BreakerAction`` for a ``CircuitBreakerId`` in the UAC registry.

    The per-archetype breaker registry can declare the same ``breaker_id`` for
    multiple archetypes with the same action; we return the first match. Returns
    ``None`` if the id is not registered (defensive — caller falls back to WARN).
    """
    for breakers in PER_ARCHETYPE_BREAKERS.values():
        for cfg in breakers:
            if cfg.breaker_id is breaker_id:
                return cfg.action
    return None


def _severity_channels(severity: AlertSeverity) -> tuple[set[str], PagerDutySeverity | None]:
    """Return (channel_set, pagerduty_severity) for an ``AlertSeverity``.

    CRITICAL → page (PagerDuty critical + Telegram); HIGH → page (PagerDuty
    warning + Telegram); WARN → Telegram; INFO → Telegram.
    """
    if severity is AlertSeverity.CRITICAL:
        return set(_PAGING_CHANNELS), "critical"
    if severity is AlertSeverity.HIGH:
        return set(_PAGING_CHANNELS), "warning"
    return {"telegram"}, None


# ── KillSwitchArmedEvent ─────────────────────────────────────────────────────
def handle_kill_switch_armed_event(event: KillSwitchArmedEvent) -> None:
    """Route a ``KillSwitchArmedEvent`` to alerting channels at the severity tier
    implied by ``event.switch_id`` (global/wallet/asset-group → CRITICAL; else HIGH).
    """
    severity = (
        AlertSeverity.CRITICAL
        if event.switch_id in _CRITICAL_ARM_SWITCH_IDS
        else AlertSeverity.HIGH
    )
    channels, pd_severity = _severity_channels(severity)
    event_name = "KILL_SWITCH_ARMED"
    details: dict[str, object] = {
        "message": (
            f"kill switch ARMED: switch={event.switch_id.value} "
            f"provenance={event.provenance.value} wallet={event.target_wallet_id} "
            f"by={event.requested_by}"
        ),
        "severity": severity.value,
        "source": "alerting-service/dr-kill-switch",
        "switch_id": event.switch_id.value,
        "provenance": event.provenance.value,
        "target_wallet_id": event.target_wallet_id,
        "requested_by": event.requested_by,
        "armed_at": event.armed_at.isoformat(),
    }
    for key, val in event.metadata.items():
        details[f"meta_{key}"] = val
    log_event(
        "DR_EVENT_ROUTED",
        details={
            "event_name": event_name,
            "switch_id": event.switch_id.value,
            "severity": severity.value,
        },
    )
    route_event_with_explicit_channels(
        event_name, details, channels=channels, pd_severity=pd_severity
    )


def handle_kill_switch_armed_payload(payload: dict[str, object]) -> None:
    """Deserialize a raw payload into a ``KillSwitchArmedEvent`` and route it."""
    log_event("KILL_SWITCH_ARM_RECEIVED", details={"switch_id": payload.get("switch_id")})
    try:
        event = KillSwitchArmedEvent.model_validate(payload)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "Dropping malformed KillSwitchArmedEvent payload: %s | payload=%s",
            exc,
            json.dumps(payload, default=str)[:512],
        )
        return
    handle_kill_switch_armed_event(event)


# ── KillSwitchDisarmEvent ────────────────────────────────────────────────────
def handle_kill_switch_disarm_event(event: KillSwitchDisarmEvent) -> None:
    """Route a ``KillSwitchDisarmEvent`` via the RECOVERY AlertCode for its
    ``recovery_mode`` (AUTO_COOLDOWN → ``KILL_SWITCH_AUTO_RECOVERED``,
    MANUAL_UNKILL → ``KILL_SWITCH_MANUAL_UNKILLED``) — both INFO → Telegram per
    the UAC-seeded routing rules.
    """
    alert_code = _RECOVERY_MODE_ALERT_CODE[event.recovery_mode]
    event_name = alert_code.value
    details: dict[str, object] = {
        "message": (
            f"kill switch DISARMED: switch={event.switch_id.value} "
            f"recovery_mode={event.recovery_mode.value} wallet={event.target_wallet_id} "
            f"by={event.disarmed_by}"
        ),
        "severity": AlertSeverity.INFO.value,
        "source": "alerting-service/dr-kill-switch",
        "switch_id": event.switch_id.value,
        "recovery_mode": event.recovery_mode.value,
        "target_wallet_id": event.target_wallet_id,
        "disarmed_by": event.disarmed_by,
        "disarmed_at": event.disarmed_at.isoformat(),
    }
    if (
        event.recovery_mode is BreakerRecoveryMode.AUTO_COOLDOWN
        and event.cooldown_seconds_elapsed is not None
    ):
        details["recovered_after_seconds"] = event.cooldown_seconds_elapsed
    if event.recovery_mode is BreakerRecoveryMode.MANUAL_UNKILL:
        details["unkilled_by_operator_id"] = event.disarmed_by
    for key, val in event.metadata.items():
        details[f"meta_{key}"] = val
    log_event(
        "DR_EVENT_ROUTED",
        details={"event_name": event_name, "switch_id": event.switch_id.value, "severity": "INFO"},
    )
    route_event(event_name, details)


def handle_kill_switch_disarm_payload(payload: dict[str, object]) -> None:
    """Deserialize a raw payload into a ``KillSwitchDisarmEvent`` and route it."""
    log_event("KILL_SWITCH_DISARM_RECEIVED", details={"switch_id": payload.get("switch_id")})
    try:
        event = KillSwitchDisarmEvent.model_validate(payload)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "Dropping malformed KillSwitchDisarmEvent payload: %s | payload=%s",
            exc,
            json.dumps(payload, default=str)[:512],
        )
        return
    handle_kill_switch_disarm_event(event)


# ── Circuit-breaker fire event (no typed UAC model yet — classify off id) ─────
def handle_circuit_breaker_fire(breaker_id: CircuitBreakerId, details: dict[str, object]) -> None:
    """Route a circuit-breaker fire to alerting channels at the tier implied by the
    breaker's ``BreakerAction`` (KILL_ALL → CRITICAL+page; CANCEL_OPEN → HIGH+page;
    SCALE_DOWN/BLOCK_NEW → WARN → Telegram).

    ``details`` is the breaker emitter's payload (e.g. trigger value, scope,
    applies_to); we enrich it with a summary + severity and route. Until a typed
    ``BreakerFiredEvent`` UAC model lands, this is the classification seam.
    """
    action = _breaker_action_for(breaker_id)
    severity = (
        _BREAKER_ACTION_SEVERITY.get(action, AlertSeverity.WARN)
        if action is not None
        else AlertSeverity.WARN
    )
    channels, pd_severity = _severity_channels(severity)
    event_name = "CIRCUIT_BREAKER_FIRED"
    action_name = action.value if action is not None else "UNKNOWN"
    default_message = f"circuit breaker FIRED: breaker={breaker_id.value} action={action_name}"
    enriched: dict[str, object] = {
        **details,
        "message": str(details.get("message") or default_message),
        "severity": severity.value,
        "source": str(details.get("source", "alerting-service/dr-breaker")),
        "breaker_id": breaker_id.value,
        "breaker_action": action.value if action is not None else None,
    }
    log_event(
        "DR_EVENT_ROUTED",
        details={
            "event_name": event_name,
            "breaker_id": breaker_id.value,
            "severity": severity.value,
        },
    )
    route_event_with_explicit_channels(
        event_name, enriched, channels=channels, pd_severity=pd_severity
    )


def handle_circuit_breaker_fire_payload(payload: dict[str, object]) -> None:
    """Deserialize a raw breaker-fire payload (must carry ``breaker_id``) and route it.

    The ``breaker_id`` field is matched against ``CircuitBreakerId`` members; an
    unknown / missing id drops the message with a structured warning rather than
    crashing the loop.
    """
    log_event("CIRCUIT_BREAKER_FIRE_RECEIVED", details={"breaker_id": payload.get("breaker_id")})
    raw_id = payload.get("breaker_id")
    try:
        breaker_id = CircuitBreakerId(str(raw_id))
    except ValueError:
        logger.warning(
            "Dropping circuit-breaker fire with unknown breaker_id=%r | payload=%s",
            raw_id,
            json.dumps(payload, default=str)[:512],
        )
        return
    handle_circuit_breaker_fire(breaker_id, payload)
