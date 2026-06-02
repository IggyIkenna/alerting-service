"""Audit-ack escalation ladder — cron that pages up the chain on SLA breach.

Per `codex/15-runbooks/alerting/audit-acknowledgement-flow.md` + the
`AuditAckSLAPolicy` (UAC `LIVE_AUDIT_ACK_POLICIES`): once an incident's audit-ack
deadline passes without a human ack, escalate through the ladder anchored at
incident-creation time:

    secondary on-call (PagerDuty) → founder (Twilio voice) → physical pager

Each step fires once (idempotent via `audit_ack_escalation_history`). The tick is
pure + injectable (`notifier=`) so it is unit-testable without live providers; the
production loop (`run_ack_escalation_loop`) just calls the tick every 30s.

Codex SSOT: `codex/04-architecture/incident-gateway-state-machine.md` § "Audit-ack queue".
Plan: `plans/active/audit_acknowledgement_sla_and_state_2026_05_23.md` Phase 2.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol

from unified_api_contracts.incident import AuditAckSLAPolicy, IncidentEnvelope, lookup_sla

from alerting_service.gateway.gateway_state import GatewayState, get_gateway_state

_logger = logging.getLogger(__name__)

STEP_SECONDARY = "secondary"
STEP_FOUNDER = "founder"
STEP_PHYSICAL_PAGER = "physical_pager"


class EscalationNotifier(Protocol):
    """Pluggable escalation sinks — the real impl wires PagerDuty / Twilio / pager."""

    def page_secondary(self, envelope: IncidentEnvelope) -> None: ...
    def call_founder(self, envelope: IncidentEnvelope) -> None: ...
    def trigger_physical_pager(self, envelope: IncidentEnvelope) -> None: ...


def _incident_age_seconds(envelope: IncidentEnvelope, now: datetime, default_seconds: int) -> float:
    """Seconds since incident creation, derived from due_at minus the default window.

    ``audit_ack_due_at == created + default_seconds``, so the escalation thresholds
    (anchored at creation) compare against ``(now - due_at) + default_seconds``.
    """
    assert envelope.audit_ack_due_at is not None
    return (now - envelope.audit_ack_due_at).total_seconds() + default_seconds


def _next_step(age_seconds: float, policy: AuditAckSLAPolicy, sent: set[str | None]) -> str | None:
    """Highest un-sent ladder step whose threshold the incident age has crossed."""
    pager_at = policy.physical_pager_after_seconds
    founder_at = policy.founder_after_seconds
    secondary_at = policy.secondary_human_after_seconds
    if pager_at is not None and age_seconds >= pager_at and STEP_PHYSICAL_PAGER not in sent:
        return STEP_PHYSICAL_PAGER
    if founder_at is not None and age_seconds >= founder_at and STEP_FOUNDER not in sent:
        return STEP_FOUNDER
    if secondary_at is not None and age_seconds >= secondary_at and STEP_SECONDARY not in sent:
        return STEP_SECONDARY
    return None


def run_ack_escalation_tick(
    state: GatewayState | None = None,
    *,
    notifier: EscalationNotifier,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """One escalation pass. Returns the list of {incident_key, step} fired this tick.

    Each incident escalates at most one step per tick (the highest un-sent step it
    has crossed); the next tick advances it further if still unacked.
    """
    state = state or get_gateway_state()
    now = now or datetime.now(UTC)
    fired: list[dict[str, str]] = []
    for env in state.pending_audit_ack():
        if env.audit_ack_due_at is None or env.audit_ack_due_at > now:
            continue
        policy = lookup_sla(env.severity_hint)
        if policy.default_seconds is None:  # INFO severity — no SLA enforcement
            continue
        age = _incident_age_seconds(env, now, policy.default_seconds)
        sent = {h.get("step") for h in env.audit_ack_escalation_history}
        step = _next_step(age, policy, sent)
        if step is None:
            continue
        if step == STEP_PHYSICAL_PAGER:
            notifier.trigger_physical_pager(env)
        elif step == STEP_FOUNDER:
            notifier.call_founder(env)
        else:
            notifier.page_secondary(env)
        state.record_escalation(env.incident_key, step)
        _logger.warning(
            "ack_escalation: incident_key=%s escalated step=%s (age=%.0fs severity=%s)",
            env.incident_key,
            step,
            age,
            env.severity_hint.value,
        )
        fired.append({"incident_key": env.incident_key, "step": step})
    return fired


class _DefaultEscalationNotifier:
    """Production escalation sinks. Routes through the Layer-3/4 fallback cascade.

    Secondary paging is a PagerDuty concern handled by the existing route_event
    path; founder + physical-pager reuse the fallback cascade (Twilio voice +
    physical pager) which already reads SM creds + config.
    """

    def page_secondary(self, envelope: IncidentEnvelope) -> None:
        _logger.warning("ack_escalation: secondary on-call page for %s", envelope.incident_key)

    def call_founder(self, envelope: IncidentEnvelope) -> None:
        from alerting_service.notifiers.incident_fallback import (
            route_incident_envelope_to_fallbacks,
        )

        route_incident_envelope_to_fallbacks(envelope, provider_in_fallback_mode=True)

    def trigger_physical_pager(self, envelope: IncidentEnvelope) -> None:
        from alerting_service.notifiers.incident_fallback import (
            route_incident_envelope_to_fallbacks,
        )

        route_incident_envelope_to_fallbacks(envelope, immediate_sev0_overrides=("ack_sla_breach_physical_pager",))


async def run_ack_escalation_loop(interval_seconds: float = 30.0) -> None:
    """Production cron: run the escalation tick every ``interval_seconds``. Never raises."""
    notifier = _DefaultEscalationNotifier()
    while True:
        try:
            run_ack_escalation_tick(notifier=notifier)
        except Exception:
            # defence-in-depth: a tick failure must not kill the escalation cron.
            _logger.warning("ack_escalation: tick failed", exc_info=True)
        await asyncio.sleep(interval_seconds)
