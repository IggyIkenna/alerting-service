"""Integration test: APPROVED LLM signoff still requires human audit-ack.

Validates the operator HARD RULE (2026-05-23): even when the AI recovery-audit
agent returns verdict=APPROVED, the incident must NOT be removed from the
audit-ack queue until a human explicitly calls audit_ack().

Plan: audit_acknowledgement_sla_and_state_2026_05_23.md Phase 4 P0.10.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from unified_api_contracts.alerting import AlertSeverity
from unified_api_contracts.incident import (
    IncidentEnvelope,
    IncidentState,
    RecoveryAuditSignoff,
    SignoffVerdict,
)

from alerting_service.gateway.gateway_state import GatewayState


def _sev2_incident(due_at: datetime) -> IncidentEnvelope:
    return IncidentEnvelope(
        event_id="evt-sev2-001",
        incident_key="inc-sev2-approved-test",
        timestamp=datetime.now(UTC),
        state=IncidentState.AUDIT_REPORT_GENERATED,
        environment="prod",
        severity_hint=AlertSeverity.HIGH,
        domain="live_trading",
        service="strategy-service",
        component="carry-engine",
        problem_type="STRATEGY_DRAWDOWN_BREACH",
        problem_summary="Carry strategy exceeded drawdown threshold",
        config_hash="sha256:def",
        code_version="v2",
        human_audit_ack_required=True,
        audit_ack_due_at=due_at,
    )


def _approved_signoff(incident_key: str) -> RecoveryAuditSignoff:
    return RecoveryAuditSignoff(
        event_id="sig-evt-001",
        parent_incident_key=incident_key,
        timestamp=datetime.now(UTC),
        agent_id="recovery-audit-agent-001",
        verdict=SignoffVerdict.APPROVED,
        narrative="Automated recovery confirmed. Drawdown was within acceptable variance.",
        layer0_action_event_ids=("act-evt-001",),
        evidence_links=("gs://audit/evidence/inc-sev2-approved-test.json",),
        llm_model="claude-sonnet-4-6",
        llm_session_id="test-session-001",
    )


class TestApprovedVerdictDoesNotBypassAuditAck:
    """APPROVED LLM verdict — incident stays in ack queue until human acks."""

    def test_approved_verdict_incident_remains_in_pending_ack(self) -> None:
        state = GatewayState()
        due_at = datetime.now(UTC) + timedelta(hours=6)
        env = _sev2_incident(due_at)
        state.register_incident(env)

        assert "inc-sev2-approved-test" in [e.incident_key for e in state.pending_audit_ack()]

        # APPROVED signoff — must NOT dequeue the incident
        state.process_signoff(_approved_signoff("inc-sev2-approved-test"))

        pending_keys = [e.incident_key for e in state.pending_audit_ack()]
        assert "inc-sev2-approved-test" in pending_keys, (
            "APPROVED verdict must NOT remove incident from the audit-ack queue "
            "(operator HARD RULE: LLM verdict is not a substitute for human ack)"
        )

    def test_approved_with_notes_verdict_also_requires_human_ack(self) -> None:
        state = GatewayState()
        due_at = datetime.now(UTC) + timedelta(hours=6)
        env = _sev2_incident(due_at)
        state.register_incident(env)

        signoff = RecoveryAuditSignoff(
            event_id="sig-evt-002",
            parent_incident_key="inc-sev2-approved-test",
            timestamp=datetime.now(UTC),
            agent_id="recovery-audit-agent-001",
            verdict=SignoffVerdict.APPROVED_WITH_NOTES,
            narrative="Recovery ok but review funding payment gap.",
            layer0_action_event_ids=("act-evt-002",),
            llm_model="claude-sonnet-4-6",
            llm_session_id="test-session-002",
        )
        state.process_signoff(signoff)

        pending_keys = [e.incident_key for e in state.pending_audit_ack()]
        assert "inc-sev2-approved-test" in pending_keys

    def test_human_audit_ack_clears_incident_from_queue(self) -> None:
        state = GatewayState()
        due_at = datetime.now(UTC) + timedelta(hours=6)
        env = _sev2_incident(due_at)
        state.register_incident(env)

        # APPROVED first — incident still pending
        state.process_signoff(_approved_signoff("inc-sev2-approved-test"))
        assert any(e.incident_key == "inc-sev2-approved-test" for e in state.pending_audit_ack())

        # Human explicitly acks — NOW it should be cleared
        result = state.audit_ack("inc-sev2-approved-test", operator_id="ikenna")
        assert result is not None
        assert result.audit_acked_by == "ikenna"
        assert result.audit_acked_at is not None

        pending_keys = [e.incident_key for e in state.pending_audit_ack()]
        assert "inc-sev2-approved-test" not in pending_keys

    def test_approved_verdict_recorded_in_signoff_history(self) -> None:
        state = GatewayState()
        due_at = datetime.now(UTC) + timedelta(hours=6)
        env = _sev2_incident(due_at)
        state.register_incident(env)
        state.process_signoff(_approved_signoff("inc-sev2-approved-test"))

        verdict = state.latest_verdict_for("inc-sev2-approved-test")
        assert verdict == SignoffVerdict.APPROVED.value

    def test_approved_incident_retains_human_audit_ack_required_true(self) -> None:
        state = GatewayState()
        due_at = datetime.now(UTC) + timedelta(hours=6)
        env = _sev2_incident(due_at)
        state.register_incident(env)
        state.process_signoff(_approved_signoff("inc-sev2-approved-test"))

        retrieved = state.get_incident("inc-sev2-approved-test")
        assert retrieved is not None
        assert retrieved.human_audit_ack_required is True
