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

import threading
from collections import deque
from datetime import UTC, datetime

from unified_api_contracts.incident import IncidentEnvelope, RecoveryAuditSignoff

from alerting_service.gateway.audit_ack_queue import AuditAckQueue

_RECENT_SIGNOFF_CAP = 200
"""DART feed shows the most recent 50; keep a slightly larger ring for filtering."""


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
