"""Audit-ack escalation-ladder cron tests.

Drives the GatewayState audit-ack queue past SLA thresholds and asserts the
escalation ladder fires secondary → founder → physical-pager in order, once each,
recording every step in audit_ack_escalation_history.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from unified_api_contracts.alerting import AlertSeverity
from unified_api_contracts.incident import IncidentEnvelope, IncidentState, lookup_sla

from alerting_service.gateway.ack_escalation import (
    STEP_FOUNDER,
    STEP_PHYSICAL_PAGER,
    STEP_SECONDARY,
    run_ack_escalation_tick,
)
from alerting_service.gateway.gateway_state import GatewayState


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def page_secondary(self, _env: IncidentEnvelope) -> None:
        self.calls.append(STEP_SECONDARY)

    def call_founder(self, _env: IncidentEnvelope) -> None:
        self.calls.append(STEP_FOUNDER)

    def trigger_physical_pager(self, _env: IncidentEnvelope) -> None:
        self.calls.append(STEP_PHYSICAL_PAGER)


def _critical_incident(state: GatewayState, due_at: datetime) -> None:
    env = IncidentEnvelope(
        event_id="evt-c",
        incident_key="inc-crit",
        timestamp=datetime.now(UTC),
        state=IncidentState.SAFE_MODE_ACTIVE,
        environment="prod",
        severity_hint=AlertSeverity.CRITICAL,
        domain="live_trading",
        service="execution-service",
        component="order-router",
        problem_type="OOM_DETECTED",
        problem_summary="OOM",
        config_hash="sha256:abc",
        code_version="v1",
        human_audit_ack_required=True,
        audit_ack_due_at=due_at,
    )
    state.register_incident(env)


def test_no_escalation_before_due() -> None:
    state = GatewayState()
    _critical_incident(state, datetime.now(UTC) + timedelta(hours=1))
    n = _RecordingNotifier()
    assert run_ack_escalation_tick(state, notifier=n, now=datetime.now(UTC)) == []
    assert n.calls == []


def test_ladder_fires_one_step_per_tick_in_order() -> None:
    # CRITICAL policy: default=300, secondary=600, founder=1800, pager=? (>=founder).
    policy = lookup_sla(AlertSeverity.CRITICAL)
    state = GatewayState()
    due = datetime.now(UTC) - timedelta(seconds=10)  # already overdue
    _critical_incident(state, due)
    n = _RecordingNotifier()

    # now far enough past creation that ALL thresholds are crossed → highest un-sent step first.
    far_now = due + timedelta(seconds=(policy.physical_pager_after_seconds or 100000) + 10)
    fired1 = run_ack_escalation_tick(state, notifier=n, now=far_now)
    fired2 = run_ack_escalation_tick(state, notifier=n, now=far_now)
    fired3 = run_ack_escalation_tick(state, notifier=n, now=far_now)
    fired4 = run_ack_escalation_tick(state, notifier=n, now=far_now)

    # Highest-first: physical_pager, then founder, then secondary, then nothing left.
    assert [f[0]["step"] for f in (fired1, fired2, fired3) if f] == [
        STEP_PHYSICAL_PAGER,
        STEP_FOUNDER,
        STEP_SECONDARY,
    ]
    assert fired4 == []
    assert set(n.calls) == {STEP_PHYSICAL_PAGER, STEP_FOUNDER, STEP_SECONDARY}
    # All three recorded in history (idempotent — no dupes).
    env = state.get_incident("inc-crit")
    assert env is not None
    steps = sorted(s for s in (h.get("step") for h in env.audit_ack_escalation_history) if s)
    assert steps == sorted([STEP_FOUNDER, STEP_PHYSICAL_PAGER, STEP_SECONDARY])


def test_secondary_only_when_just_past_secondary_threshold() -> None:
    policy = lookup_sla(AlertSeverity.CRITICAL)
    state = GatewayState()
    due = datetime.now(UTC) - timedelta(seconds=1)
    _critical_incident(state, due)
    n = _RecordingNotifier()
    # age = (now - due) + default. Pick now so age is just over secondary, under founder.
    secondary = policy.secondary_human_after_seconds or 600
    target_age = secondary + 5
    now = due + timedelta(seconds=target_age - (policy.default_seconds or 300))
    fired = run_ack_escalation_tick(state, notifier=n, now=now)
    assert [f["step"] for f in fired] == [STEP_SECONDARY]
    assert n.calls == [STEP_SECONDARY]
