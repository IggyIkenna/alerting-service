"""Process-wide Incident Gateway state — the in-memory holder the HTTP routes read.

The Tier-2 scaffold keeps gateway state in-process (durable across restarts via
the GCS ``incident_persister`` snapshots, rehydrated on cold start). This module
is the single holder the FastAPI ``safety_ops`` routes consult:

- ``incidents`` — latest IncidentEnvelope snapshot per ``incident_key``.
- ``audit_ack_queue`` — the :class:`AuditAckQueue` (min-heap on ``audit_ack_due_at``).
- ``recent_signoffs`` — bounded ring of the most recent RecoveryAuditSignoff rows
  (the LLM recovery-audit-signoff agent appends here as it signs off; the DART
  "LLM Audit Verdicts" feed reads the last 50).

Production swaps the in-memory structures for Redis Streams + the GCS audit store
(same accessor surface). Keep this holder the ONLY mutable gateway singleton so
the route layer never reaches into module globals scattered across the gateway.

Codex SSOT: ``codex/04-architecture/incident-gateway-state-machine.md``.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime, timedelta

from unified_api_contracts.alerting import AlertSeverity
from unified_api_contracts.incident import (
    ALLOWED_TRANSITIONS,
    IncidentEnvelope,
    IncidentState,
    RecoveryAuditSignoff,
    SignoffVerdict,
)

from alerting_service.gateway.audit_ack_queue import AuditAckQueue
from alerting_service.gateway.state_machine import IncidentStateMachine

_logger = logging.getLogger(__name__)

_RECENT_SIGNOFF_CAP = 200
"""DART feed shows the most recent 50; keep a slightly larger ring for filtering."""

_ESCALATE_TO_HUMAN_ACK_SECONDS = 3600
"""ESCALATE_TO_HUMAN verdict shortens the audit-ack window to 1h (vs default 6h)."""


def _shortest_transition_path(start: IncidentState, target: IncidentState) -> list[IncidentState] | None:
    """BFS over ALLOWED_TRANSITIONS; returns the state sequence start→...→target (excl. start).

    Returns ``[]`` if already at target, or ``None`` if target is unreachable.
    """
    if start == target:
        return []
    visited = {start}
    queue: deque[tuple[IncidentState, list[IncidentState]]] = deque([(start, [])])
    while queue:
        node, path = queue.popleft()
        for nxt in ALLOWED_TRANSITIONS[node]:
            if nxt in visited:
                continue
            new_path = [*path, nxt]
            if nxt == target:
                return new_path
            visited.add(nxt)
            queue.append((nxt, new_path))
    return None


class GatewayState:
    """Thread-safe holder for process-wide Incident Gateway state.

    Construct ONCE per process via :func:`get_gateway_state`. The state machine's
    persist callback (and the recovery-audit-signoff ingest path) write here; the
    HTTP routes read here.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._incidents: dict[str, IncidentEnvelope] = {}
        self._recent_signoffs: deque[RecoveryAuditSignoff] = deque(maxlen=_RECENT_SIGNOFF_CAP)
        self.audit_ack_queue = AuditAckQueue()
        self.state_machine = IncidentStateMachine(persist=self._persist_snapshot)

    def _persist_snapshot(self, envelope: IncidentEnvelope, _previous: IncidentState) -> None:
        """State-machine persist hook — keep the registry as the latest snapshot."""
        with self._lock:
            self._incidents[envelope.incident_key] = envelope

    # ── Incident registry ──────────────────────────────────────────────────

    def register_incident(self, envelope: IncidentEnvelope) -> None:
        """Upsert the latest snapshot for an incident + (re)enqueue if it needs audit-ack."""
        with self._lock:
            self._incidents[envelope.incident_key] = envelope
        # enqueue is idempotent + thread-safe internally; only enqueues when the
        # envelope carries a deadline and has not yet been audit-acked.
        self.audit_ack_queue.enqueue(envelope)

    def get_incident(self, incident_key: str) -> IncidentEnvelope | None:
        with self._lock:
            return self._incidents.get(incident_key)

    # ── Acks ───────────────────────────────────────────────────────────────

    def operational_ack(self, incident_key: str, operator_id: str) -> IncidentEnvelope | None:
        """Record operational-ack. Returns the updated snapshot, or None if incident unknown."""
        with self._lock:
            env = self._incidents.get(incident_key)
            if env is None:
                return None
            updated = env.model_copy(
                update={
                    "operational_acked_by": operator_id,
                    "operational_acked_at": datetime.now(UTC),
                }
            )
            self._incidents[incident_key] = updated
            return updated

    def audit_ack(self, incident_key: str, operator_id: str) -> IncidentEnvelope | None:
        """Record audit-ack on the incident + remove it from the audit-ack queue.

        Even APPROVED LLM verdicts require this human audit-ack (operator HARD RULE
        2026-05-23), so this is the action that clears the SLA countdown.
        """
        with self._lock:
            env = self._incidents.get(incident_key)
            if env is None:
                return None
            updated = env.model_copy(
                update={
                    "audit_acked_by": operator_id,
                    "audit_acked_at": datetime.now(UTC),
                }
            )
            self._incidents[incident_key] = updated
        self.audit_ack_queue.dequeue(incident_key)
        return updated

    # ── Recovery-audit signoffs ──────────────────────────────────────────────

    def record_signoff(self, signoff: RecoveryAuditSignoff) -> None:
        with self._lock:
            self._recent_signoffs.appendleft(signoff)

    def recent_signoffs(self, limit: int = 50) -> list[RecoveryAuditSignoff]:
        with self._lock:
            return list(self._recent_signoffs)[: max(0, limit)]

    def pending_audit_ack(self) -> list[IncidentEnvelope]:
        """Incidents still awaiting human audit-ack (in the queue + not yet acked)."""
        result: list[IncidentEnvelope] = []
        for incident_key in self.audit_ack_queue.pending_keys():
            env = self.get_incident(incident_key)
            if env is not None and env.audit_acked_at is None:
                result.append(env)
        return result

    def latest_verdict_for(self, incident_key: str) -> str | None:
        """Most recent signoff verdict for an incident (for the audit-ack queue display)."""
        with self._lock:
            for s in self._recent_signoffs:
                if s.parent_incident_key == incident_key:
                    return s.verdict.value
        return None

    # ── Verdict-driven gateway reactions ─────────────────────────────────────

    def process_signoff(self, signoff: RecoveryAuditSignoff) -> IncidentEnvelope | None:
        """Record the signoff + drive the verdict-mandated gateway reaction.

        - DISPUTE_AUTOMATED_ACTION → force the incident to SAFE_MODE_ACTIVE + SEV0
          (CRITICAL), regardless of any recovery-confirmed signal.
        - ESCALATE_TO_HUMAN → require human operational ack + shorten the audit-ack
          window to 1h so the human is paged sooner.
        - APPROVED / APPROVED_WITH_NOTES → no forced transition; the incident stays
          in the audit-ack queue until a human acks (operator HARD RULE — even an
          APPROVED LLM verdict is not a substitute for human audit-ack).

        Returns the updated envelope, or None if the parent incident is unknown.
        """
        self.record_signoff(signoff)
        env = self.get_incident(signoff.parent_incident_key)
        if env is None:
            return None
        if signoff.verdict == SignoffVerdict.DISPUTE_AUTOMATED_ACTION:
            return self._force_safe_mode(env, signoff)
        if signoff.verdict == SignoffVerdict.ESCALATE_TO_HUMAN:
            return self._shorten_ack_window(env, signoff)
        return env

    def _force_safe_mode(self, env: IncidentEnvelope, signoff: RecoveryAuditSignoff) -> IncidentEnvelope:
        """Drive the incident to SAFE_MODE_ACTIVE + SEV0 via the shortest allowed path."""
        path = _shortest_transition_path(env.state, IncidentState.SAFE_MODE_ACTIVE)
        if path is None:
            _logger.warning(
                "process_signoff: DISPUTE on incident_key=%s but SAFE_MODE_ACTIVE unreachable "
                "from state=%s — leaving state, raising severity only",
                env.incident_key,
                env.state.value,
            )
            updated = env.model_copy(update={"severity_hint": AlertSeverity.CRITICAL})
            self._persist_snapshot(updated, env.state)
            return updated
        current = env
        history_entry: dict[str, str] = {
            "event": "DISPUTE_FORCED_SAFE_MODE",
            "signoff_event_id": signoff.event_id,
            "at": datetime.now(UTC).isoformat(),
        }
        for i, target in enumerate(path):
            if i == len(path) - 1:
                # Final hop lands in SAFE_MODE_ACTIVE — stamp SEV0 + escalation history.
                result = self.state_machine.transition(
                    current,
                    target,
                    severity_hint=AlertSeverity.CRITICAL,
                    audit_ack_escalation_history=(
                        *current.audit_ack_escalation_history,
                        history_entry,
                    ),
                )
            else:
                result = self.state_machine.transition(current, target)
            if not result.succeeded:
                _logger.warning(
                    "process_signoff: forced transition %s → %s failed: %s",
                    current.state.value,
                    target.value,
                    result.failure_reason,
                )
                break
            current = result.envelope
        return current

    def record_escalation(self, incident_key: str, step: str) -> IncidentEnvelope | None:
        """Append an escalation-ladder step to the incident's history. Idempotent per step."""
        with self._lock:
            env = self._incidents.get(incident_key)
            if env is None:
                return None
            if any(h.get("step") == step for h in env.audit_ack_escalation_history):
                return env  # already recorded this step
            updated = env.model_copy(
                update={
                    "audit_ack_escalation_history": (
                        *env.audit_ack_escalation_history,
                        {
                            "event": "ESCALATION_FIRED",
                            "step": step,
                            "at": datetime.now(UTC).isoformat(),
                        },
                    ),
                }
            )
            self._incidents[incident_key] = updated
            return updated

    def _shorten_ack_window(self, env: IncidentEnvelope, signoff: RecoveryAuditSignoff) -> IncidentEnvelope:
        """ESCALATE_TO_HUMAN — require operational ack + shorten audit-ack deadline to 1h."""
        new_due = datetime.now(UTC) + timedelta(seconds=_ESCALATE_TO_HUMAN_ACK_SECONDS)
        with self._lock:
            current = self._incidents.get(env.incident_key, env)
            updated = current.model_copy(
                update={
                    "human_operational_ack_required": True,
                    "audit_ack_due_at": new_due,
                    "audit_ack_escalation_history": (
                        *current.audit_ack_escalation_history,
                        {
                            "event": "ESCALATE_TO_HUMAN_SHORTENED_ACK",
                            "signoff_event_id": signoff.event_id,
                            "at": datetime.now(UTC).isoformat(),
                        },
                    ),
                }
            )
            self._incidents[env.incident_key] = updated
        # Re-enqueue with the shortened deadline (dequeue first so enqueue is not a no-op).
        self.audit_ack_queue.dequeue(env.incident_key)
        self.audit_ack_queue.enqueue(updated)
        return updated


_state: GatewayState | None = None
_state_lock = threading.Lock()


def get_gateway_state() -> GatewayState:
    """Return the process-wide GatewayState, constructing it on first call."""
    global _state
    if _state is None:
        with _state_lock:
            if _state is None:
                _state = GatewayState()
    return _state


def reset_gateway_state_for_testing() -> None:
    """Test-only — drop the process-wide state so each test starts clean."""
    global _state
    with _state_lock:
        _state = None
