"""IncidentStateMachine — drives IncidentEnvelope state transitions.

Wraps the closed-set ``ALLOWED_TRANSITIONS`` from UAC + enforces the central
invariant ``AUTO_ACTION_SUCCEEDED → RESOLVED is FORBIDDEN`` at every write
site. Emitters MUST go through this class to update incident state.

Codex SSOT: ``codex/04-architecture/incident-gateway-state-machine.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from unified_api_contracts.incident import (
    ALLOWED_TRANSITIONS,
    IllegalIncidentTransitionError,
    IncidentEnvelope,
    IncidentState,
    assert_allowed_transition,
)

_logger = logging.getLogger(__name__)


@dataclass
class TransitionResult:  # CORRECT-LOCAL: internal state-machine result type
    """Outcome of an attempted transition."""

    envelope: IncidentEnvelope
    """The new envelope snapshot after the transition."""

    previous_state: IncidentState
    """State BEFORE the transition (for audit trail)."""

    succeeded: bool
    """False iff the transition was rejected (caller may retry with different target)."""

    failure_reason: str | None
    """Free-text reason when succeeded=False."""


PersistCallback = Callable[[IncidentEnvelope, IncidentState], None]
"""Caller-supplied hook fired after a successful transition. Used by
``alerting_service/gateway/incident_persister.py`` to write the snapshot to GCS."""


class IncidentStateMachine:
    """Drives IncidentEnvelope state transitions.

    Construct ONCE per process. The state machine itself is stateless beyond
    holding the persist callback; per-incident state lives in IncidentEnvelope.

    Usage::

        sm = IncidentStateMachine(persist=_write_to_gcs)
        new_envelope = sm.transition(env, IncidentState.AUTO_ACTION_STARTED).envelope
    """

    def __init__(self, persist: PersistCallback | None = None) -> None:
        self._persist = persist

    def transition(
        self,
        envelope: IncidentEnvelope,
        target: IncidentState,
        *,
        new_event_id: str | None = None,
        **field_overrides: object,
    ) -> TransitionResult:
        """Attempt ``envelope.state → target`` and return a TransitionResult.

        ``field_overrides`` update the new immutable snapshot (e.g.
        ``auto_action_taken``, ``audit_ack_due_at``). ``succeeded=True`` iff the
        transition was allowed. Central invariant (enforced by
        ``assert_allowed_transition``): ``AUTO_ACTION_SUCCEEDED → RESOLVED`` is
        FORBIDDEN — recovery verification must intervene.
        """
        previous = envelope.state
        try:
            assert_allowed_transition(previous, target)
        except IllegalIncidentTransitionError as exc:
            return TransitionResult(
                envelope=envelope, previous_state=previous, succeeded=False, failure_reason=str(exc)
            )

        # Build new immutable snapshot (IncidentEnvelope is frozen → model_copy).
        update_kwargs: dict[str, object] = {
            "state": target,
            "timestamp": datetime.now(UTC),
            **field_overrides,
        }
        if new_event_id is not None:
            update_kwargs["event_id"] = new_event_id

        new_envelope = envelope.model_copy(update=update_kwargs)
        self._run_persist(new_envelope, previous, target)
        return TransitionResult(envelope=new_envelope, previous_state=previous, succeeded=True, failure_reason=None)

    def _run_persist(self, new_envelope: IncidentEnvelope, previous: IncidentState, target: IncidentState) -> None:
        """Fire the persist callback if set; never propagate its failures."""
        if self._persist is None:
            return
        try:
            self._persist(new_envelope, previous)
        except Exception:
            _logger.warning(
                "IncidentStateMachine persist callback failed for incident_key=%s state=%s → %s",
                new_envelope.incident_key,
                previous.value,
                target.value,
                exc_info=True,
            )

    @staticmethod
    def is_terminal(state: IncidentState) -> bool:
        """True iff no transitions out of this state are allowed."""
        return len(ALLOWED_TRANSITIONS[state]) == 0
