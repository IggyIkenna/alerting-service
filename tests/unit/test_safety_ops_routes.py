"""Unit tests for the Safety Ops HTTP routes (alerting_service.api.routes.safety_ops).

Covers both modes:
- mock mode (CLOUD_MOCK_MODE default in tests) — deterministic fixtures render the tab.
- live mode — register an incident + signoff into GatewayState, assert the queue /
  feed / ack flow drives the in-memory holder correctly (incl. the audit-ack clearing
  the SLA-countdown queue entry).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from unified_api_contracts.alerting import AlertSeverity
from unified_api_contracts.incident import (
    IncidentEnvelope,
    IncidentState,
    RecoveryAuditSignoff,
    SignoffVerdict,
)
from unified_trading_library import AuthContext

from alerting_service.api.main import _api_auth, app
from alerting_service.api.routes import safety_ops
from alerting_service.gateway.gateway_state import (
    get_gateway_state,
    reset_gateway_state_for_testing,
)


class _LiveCfg:
    """Stand-in for AlertingSystemConfig that forces live (non-mock) mode."""

    @staticmethod
    def is_mock_mode() -> bool:
        return False


def _fake_auth() -> AuthContext:
    return AuthContext(org_id="internal", user_id="test", role="admin", is_internal=True, is_api_key=True)


@pytest.fixture
def client() -> TestClient:
    # Bypass API-key auth for the route under test.
    app.dependency_overrides[_api_auth] = _fake_auth
    reset_gateway_state_for_testing()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        reset_gateway_state_for_testing()


def _envelope(incident_key: str, **overrides: object) -> IncidentEnvelope:
    defaults: dict[str, object] = {
        "event_id": f"evt-{incident_key}",
        "incident_key": incident_key,
        "timestamp": datetime.now(UTC),
        "state": IncidentState.AUTO_ACTION_SUCCEEDED,
        "environment": "prod",
        "severity_hint": AlertSeverity.HIGH,
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


def _signoff(incident_key: str, verdict: SignoffVerdict) -> RecoveryAuditSignoff:
    return RecoveryAuditSignoff(
        event_id=f"signoff-{incident_key}",
        parent_incident_key=incident_key,
        timestamp=datetime.now(UTC),
        agent_id="recovery-audit-signoff",
        verdict=verdict,
        narrative="auto-recovery reviewed",
        layer0_action_event_ids=(f"evt-{incident_key}",),
        evidence_links=(),
        llm_model="claude-sonnet-4-6",
        llm_session_id="sess-1",
    )


# ── Mock mode ──────────────────────────────────────────────────────────────


def test_audit_ack_queue_mock_mode(client: TestClient) -> None:
    resp = client.get("/safety-ops/audit-ack-queue?limit=50")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list) and len(rows) >= 1
    assert {r["incident_key"] for r in rows}  # all have keys
    assert all("audit_ack_due_at" in r and "severity" in r for r in rows)


def test_recovery_signoffs_mock_mode(client: TestClient) -> None:
    resp = client.get("/safety-ops/recovery-audit-signoffs?limit=50")
    assert resp.status_code == 200
    verdicts = {r["verdict"] for r in resp.json()}
    assert "APPROVED" in verdicts
    assert "DISPUTE_AUTOMATED_ACTION" in verdicts


def test_acks_mock_mode(client: TestClient) -> None:
    op = client.post("/safety-ops/incidents/inc-x/operational-ack")
    audit = client.post("/safety-ops/incidents/inc-x/audit-ack")
    assert op.status_code == 200 and op.json()["ack_type"] == "operational"
    assert audit.status_code == 200 and audit.json()["ack_type"] == "audit"


# ── Live mode (in-memory GatewayState) ─────────────────────────────────────


def test_live_queue_feed_and_audit_ack_clears_entry(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety_ops, "_cfg", _LiveCfg())
    state = get_gateway_state()
    state.register_incident(_envelope("inc-live-1"))
    state.record_signoff(_signoff("inc-live-1", SignoffVerdict.APPROVED))

    # Queue shows the incident enriched with verdict + service + problem_type.
    rows = client.get("/safety-ops/audit-ack-queue").json()
    assert [r["incident_key"] for r in rows] == ["inc-live-1"]
    assert rows[0]["llm_verdict"] == "APPROVED"
    assert rows[0]["service"] == "execution-service"

    # Feed shows the signoff.
    feed = client.get("/safety-ops/recovery-audit-signoffs").json()
    assert feed[0]["parent_incident_key"] == "inc-live-1"

    # Audit-ack clears the SLA-countdown queue entry.
    acked = client.post("/safety-ops/incidents/inc-live-1/audit-ack?operator_id=ikenna@odum-research.com")
    assert acked.status_code == 200
    assert client.get("/safety-ops/audit-ack-queue").json() == []


def test_live_unknown_incident_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety_ops, "_cfg", _LiveCfg())
    resp = client.post("/safety-ops/incidents/nope/audit-ack")
    assert resp.status_code == 404


def test_live_signoff_ingest_dispute_forces_safe_mode(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety_ops, "_cfg", _LiveCfg())
    state = get_gateway_state()
    state.register_incident(_envelope("inc-sig", state=IncidentState.AUTO_ACTION_SUCCEEDED))
    body = _signoff("inc-sig", SignoffVerdict.DISPUTE_AUTOMATED_ACTION).model_dump(mode="json")
    resp = client.post("/safety-ops/signoffs", json=body)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["verdict"] == "DISPUTE_AUTOMATED_ACTION"
    assert payload["resulting_state"] == "SAFE_MODE_ACTIVE"
