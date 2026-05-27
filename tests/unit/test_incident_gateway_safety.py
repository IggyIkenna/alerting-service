"""Incident-gateway safety surface — manual-action validation, audit-ack queue,
GatewayState acks, and the Layer-3/4 fallback cascade.

These cover the human-override + escalation paths that back the Safety Ops tab so
the gateway is exercised end-to-end (not just the happy-path HTTP routes).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from unified_api_contracts.alerting import AlertSeverity
from unified_api_contracts.incident import (
    ActionType,
    IncidentEnvelope,
    IncidentState,
    RecoveryAuditSignoff,
    SignoffVerdict,
)

from alerting_service.api.main import app
from alerting_service.auth import verify_api_key
from alerting_service.gateway import manual_action_endpoint as mae
from alerting_service.gateway.audit_ack_queue import AuditAckQueue
from alerting_service.gateway.gateway_state import GatewayState
from alerting_service.notifiers import incident_fallback as nfallback
from alerting_service.notifiers.physical_pager import PagerNotifierResult


def _envelope(incident_key: str, **overrides: object) -> IncidentEnvelope:
    defaults: dict[str, object] = {
        "event_id": f"evt-{incident_key}",
        "incident_key": incident_key,
        "timestamp": datetime.now(UTC),
        "state": IncidentState.AUTO_ACTION_SUCCEEDED,
        "environment": "prod",
        "severity_hint": AlertSeverity.CRITICAL,
        "domain": "live_trading",
        "service": "execution-service",
        "component": "order-router",
        "problem_type": "OOM_DETECTED",
        "problem_summary": "Service OOM",
        "config_hash": "sha256:abc",
        "code_version": "v1.0.0",
        "human_audit_ack_required": True,
        "audit_ack_due_at": datetime.now(UTC) + timedelta(hours=2),
    }
    defaults.update(overrides)
    return IncidentEnvelope(**defaults)  # type: ignore[arg-type]


# ── Manual-action endpoint validation ───────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    app.dependency_overrides[verify_api_key] = lambda: "test"
    mae._LAST_ACTION_AT.clear()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        mae._LAST_ACTION_AT.clear()


def _action_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "action_type": "cancel_open_orders",
        "scope": {"venue": "binance"},
        "reason": "manual test",
        "operator_id": "ikenna@odum-research.com",
        "confirm_string": "CANCEL_ALL_binance",
    }
    body.update(overrides)
    return body


def test_manual_action_rejects_unknown_operator(client: TestClient) -> None:
    resp = client.post("/admin/manual-action", json=_action_body(operator_id="mallory@evil.com"))
    assert resp.status_code == 403


def test_manual_action_rejects_confirm_string_mismatch(client: TestClient) -> None:
    resp = client.post("/admin/manual-action", json=_action_body(confirm_string="WRONG"))
    assert resp.status_code == 400
    assert resp.json()["detail"]["expected"] == "CANCEL_ALL_binance"


def test_manual_action_rate_limited_on_second_call(client: TestClient) -> None:
    # First call passes allowlist + rate-limit, then 400s on bad confirm (consumes a token).
    first = client.post("/admin/manual-action", json=_action_body(confirm_string="WRONG"))
    assert first.status_code == 400
    second = client.post("/admin/manual-action", json=_action_body(confirm_string="WRONG"))
    assert second.status_code == 429


def test_expected_confirm_string_helper() -> None:
    assert mae.expected_confirm_string(mae.ActionType.CANCEL_OPEN_ORDERS, {"venue": "binance"}) == (
        "CANCEL_ALL_binance"
    )


# ── P0.12: every action_type x scope combo — registry completeness + typo rejection ──


_ALL_ACTION_COMBOS: list[tuple[ActionType, dict[str, str], str]] = [
    (ActionType.RESTART_SERVICE, {"service": "execution-service"}, "RESTART_execution-service"),
    (
        ActionType.RESTART_CONTAINER,
        {"container": "strategy-live-01"},
        "RESTART_CONTAINER_strategy-live-01",
    ),
    (
        ActionType.REDEPLOY_KNOWN_GOOD,
        {"service": "execution-service", "to_revision": "abc123"},
        "REDEPLOY_execution-service_to_abc123",
    ),
    (ActionType.RESIZE_MACHINE_AFTER_OOM, {"vm": "strategy-live-01"}, "RESIZE_strategy-live-01"),
    (
        ActionType.FAILOVER_FEED,
        {"venue": "binance", "primary": "feed-1", "backup": "feed-2"},
        "FAILOVER_binance_feed-1_to_feed-2",
    ),
    (ActionType.PAUSE_STRATEGY, {"strategy": "carry_staked_basis"}, "PAUSE_carry_staked_basis"),
    (ActionType.CANCEL_OPEN_ORDERS, {"venue": "binance"}, "CANCEL_ALL_binance"),
    (ActionType.DISABLE_VENUE, {"venue": "bybit"}, "DISABLE_bybit"),
    (
        ActionType.ENTER_SAFE_MODE,
        {"strategy": "arbitrage_price_dispersion"},
        "SAFE_MODE_arbitrage_price_dispersion",
    ),
    (
        ActionType.ENTER_READONLY_RECON_MODE,
        {"service": "strategy-service"},
        "READONLY_strategy-service",
    ),
]


def test_confirm_string_registry_covers_all_action_types() -> None:
    """Every ActionType member must have a template — registry completeness guard."""
    for action_type in ActionType:
        assert action_type in mae._CONFIRM_STRING_TEMPLATES, f"No confirm-string template for {action_type}"


@pytest.mark.parametrize("action_type,scope,expected", _ALL_ACTION_COMBOS)
def test_expected_confirm_string_all_combos(action_type: ActionType, scope: dict[str, str], expected: str) -> None:
    assert mae.expected_confirm_string(action_type, scope) == expected


@pytest.mark.parametrize("action_type,scope,expected", _ALL_ACTION_COMBOS)
def test_confirm_string_typo_rejected_all_action_types(
    client: TestClient, action_type: ActionType, scope: dict[str, str], expected: str
) -> None:
    body: dict[str, object] = {
        "action_type": action_type.value,
        "scope": scope,
        "reason": "test",
        "operator_id": "ikenna@odum-research.com",
        "confirm_string": "WRONG_STRING",
    }
    resp = client.post("/admin/manual-action", json=body)
    assert resp.status_code == 400
    assert resp.json()["detail"]["expected"] == expected


# ── AuditAckQueue internals ──────────────────────────────────────────────────


def test_audit_ack_queue_enqueue_dequeue_due_soon() -> None:
    q = AuditAckQueue()
    overdue = _envelope("inc-overdue", audit_ack_due_at=datetime.now(UTC) - timedelta(minutes=1))
    future = _envelope("inc-future", audit_ack_due_at=datetime.now(UTC) + timedelta(hours=1))
    assert q.enqueue(overdue) is not None
    assert q.enqueue(future) is not None
    assert q.enqueue(overdue) is None  # idempotent re-enqueue
    assert q.size() == 2
    assert set(q.pending_keys()) == {"inc-overdue", "inc-future"}
    assert q.peek_due_at() is not None

    due = list(q.due_soon(datetime.now(UTC)))
    assert [e.incident_key for e in due] == ["inc-overdue"]

    assert q.dequeue("inc-future") is True
    assert q.dequeue("inc-future") is False
    assert q.size() == 0


def test_audit_ack_queue_skips_already_acked() -> None:
    q = AuditAckQueue()
    acked = _envelope("inc-acked", audit_acked_at=datetime.now(UTC))
    no_deadline = _envelope("inc-nodl", audit_ack_due_at=None)
    assert q.enqueue(acked) is None
    assert q.enqueue(no_deadline) is None


# ── GatewayState acks + signoffs ─────────────────────────────────────────────


def _signoff(incident_key: str, verdict: SignoffVerdict) -> RecoveryAuditSignoff:
    return RecoveryAuditSignoff(
        event_id=f"signoff-{incident_key}",
        parent_incident_key=incident_key,
        timestamp=datetime.now(UTC),
        agent_id="recovery-audit-signoff",
        verdict=verdict,
        narrative="reviewed",
        layer0_action_event_ids=(f"evt-{incident_key}",),
        evidence_links=(),
        llm_model="claude-sonnet-4-6",
        llm_session_id="s1",
    )


def test_gateway_state_operational_ack_and_verdict() -> None:
    state = GatewayState()
    state.register_incident(_envelope("inc-1"))
    state.record_signoff(_signoff("inc-1", SignoffVerdict.ESCALATE_TO_HUMAN))

    updated = state.operational_ack("inc-1", "ikenna@odum-research.com")
    assert updated is not None
    assert updated.operational_acked_by == "ikenna@odum-research.com"
    assert updated.operational_acked_at is not None

    assert state.latest_verdict_for("inc-1") == "ESCALATE_TO_HUMAN"
    assert state.latest_verdict_for("unknown") is None
    assert state.operational_ack("unknown", "x") is None
    assert [s.parent_incident_key for s in state.recent_signoffs(limit=1)] == ["inc-1"]


def test_gateway_state_audit_ack_clears_queue() -> None:
    state = GatewayState()
    state.register_incident(_envelope("inc-2"))
    assert state.audit_ack_queue.size() == 1
    assert state.audit_ack("inc-2", "ikenna@odum-research.com") is not None
    assert state.audit_ack_queue.size() == 0
    assert state.pending_audit_ack() == []


# ── Verdict-driven gateway reactions ─────────────────────────────────────────


def test_dispute_forces_safe_mode_and_sev0() -> None:
    state = GatewayState()
    # Incident sitting in AUTO_ACTION_SUCCEEDED (LLM disputes a "successful" auto-action).
    state.register_incident(_envelope("inc-d", state=IncidentState.AUTO_ACTION_SUCCEEDED))
    result = state.process_signoff(_signoff("inc-d", SignoffVerdict.DISPUTE_AUTOMATED_ACTION))
    assert result is not None
    # Forced through RECOVERY_VERIFICATION_STARTED → RECOVERY_UNCERTAIN → SAFE_MODE_ACTIVE.
    assert result.state is IncidentState.SAFE_MODE_ACTIVE
    assert result.severity_hint is AlertSeverity.CRITICAL
    assert any(h.get("event") == "DISPUTE_FORCED_SAFE_MODE" for h in result.audit_ack_escalation_history)


def test_escalate_to_human_shortens_ack_window() -> None:
    state = GatewayState()
    far_due = datetime.now(UTC) + timedelta(hours=6)
    state.register_incident(_envelope("inc-e", audit_ack_due_at=far_due))
    result = state.process_signoff(_signoff("inc-e", SignoffVerdict.ESCALATE_TO_HUMAN))
    assert result is not None
    assert result.human_operational_ack_required is True
    # New deadline is ~1h out, well before the original 6h.
    assert result.audit_ack_due_at is not None and result.audit_ack_due_at < far_due


def test_approved_does_not_transition_or_clear_queue() -> None:
    state = GatewayState()
    state.register_incident(_envelope("inc-a", state=IncidentState.AUTO_ACTION_SUCCEEDED))
    result = state.process_signoff(_signoff("inc-a", SignoffVerdict.APPROVED))
    assert result is not None
    # APPROVED is informational — no forced transition, incident stays awaiting human ack.
    assert result.state is IncidentState.AUTO_ACTION_SUCCEEDED
    assert state.audit_ack_queue.size() == 1


def test_process_signoff_unknown_incident_returns_none() -> None:
    state = GatewayState()
    assert state.process_signoff(_signoff("nope", SignoffVerdict.APPROVED)) is None


# ── Fallback cascade (route_incident_envelope_to_fallbacks) ──────────────────


def test_fallback_cascade_not_configured_returns_all_false() -> None:
    env = _envelope("inc-crit")
    result = nfallback.route_incident_envelope_to_fallbacks(env)
    # CRITICAL fires the twilio-voice + pager intents, but neither is configured.
    assert result == {"twilio_voice": False, "twilio_sms": False, "physical_pager": False}


def test_fallback_cascade_configured_fires_voice_and_pager(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cfg = SimpleNamespace(
        twilio_account_sid="sid",
        twilio_auth_token="tok",
        twilio_from_number="+100",
        twilio_to_number_primary="+200",
        physical_pager_vendor_name="WEBHOOK",
        physical_pager_endpoint_url="https://pager.example/hook",
        physical_pager_auth_header="Bearer x",
        physical_pager_to_number="+300",
    )
    monkeypatch.setattr(nfallback, "_get_cloud_config", lambda: fake_cfg)
    monkeypatch.setattr(nfallback, "get_paging_credentials", lambda: {})

    import alerting_service.notifiers.twilio_voice as tv

    monkeypatch.setattr(
        tv,
        "send_twilio_voice",
        lambda **_: tv.TwilioVoiceResult(ok=True, call_sid="CA1", http_status=200, error_message=None),
    )

    class _FakePager:
        def __init__(self, **_: object) -> None:
            pass

        def send(self, **_: object) -> PagerNotifierResult:
            return PagerNotifierResult(ok=True, vendor_message_id="m1", http_status=200, error_message=None)

    import alerting_service.notifiers.physical_pager as pp

    monkeypatch.setattr(pp, "get_physical_pager_class", lambda _name: _FakePager)

    result = nfallback.route_incident_envelope_to_fallbacks(_envelope("inc-crit"))
    assert result["twilio_voice"] is True
    assert result["physical_pager"] is True
