"""Safety Ops HTTP routes — backend for the DART/deployment-ui Safety Ops tab.

Exposes the Incident Gateway's human-facing surface:

- ``GET  /safety-ops/recovery-audit-signoffs`` — last N LLM RecoveryAuditSignoff rows.
- ``GET  /safety-ops/audit-ack-queue``         — incidents pending human audit-ack.
- ``POST /safety-ops/incidents/{key}/operational-ack`` — operator clears the operational ack.
- ``POST /safety-ops/incidents/{key}/audit-ack``       — operator clears the audit ack (the
  one that stops the SLA countdown; required even for APPROVED LLM verdicts).

The unified-trading-system-ui Next.js ``/api/safety-ops/*`` route handlers proxy to
these endpoints; the three widgets (LlmAuditVerdictsFeed / AuditAckQueueWidget /
Layer0Panel) render the responses.

Mock mode (``CLOUD_MOCK_MODE=true``) returns deterministic sample data so the tab
renders without a populated gateway — matches the UI mock-handler fixtures.

Codex SSOT: ``codex/04-architecture/incident-gateway-state-machine.md`` +
``codex/04-architecture/safety-ops-tab.md``.
Plan: ``plans/active/deployment_ui_safety_ops_tab_2026_05_23.md`` Phase 2 (backend proxy).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from unified_api_contracts.incident import IncidentEnvelope, RecoveryAuditSignoff

from alerting_service.config import AlertingSystemConfig
from alerting_service.gateway.gateway_state import get_gateway_state

router = APIRouter(prefix="/safety-ops", tags=["safety-ops"])
_cfg = AlertingSystemConfig()


# ── Response schemas (match the UI widget shapes) ──────────────────────────


class SignoffRow(BaseModel):  # CORRECT-LOCAL: HTTP response DTO for /safety-ops route
    event_id: str
    parent_incident_key: str
    timestamp: str
    verdict: str
    narrative: str
    llm_model: str
    confidence: float | None


class AuditAckRow(BaseModel):  # CORRECT-LOCAL: HTTP response DTO for /safety-ops route
    incident_key: str
    audit_ack_due_at: str
    severity: str
    problem_type: str
    service: str
    llm_verdict: str | None


class AckResponse(BaseModel):  # CORRECT-LOCAL: HTTP response DTO for /safety-ops route
    ok: bool
    incident_key: str
    ack_type: str
    acked_at: str


class SignoffIngestResponse(BaseModel):  # CORRECT-LOCAL: HTTP response DTO for /safety-ops route
    ok: bool
    incident_key: str
    verdict: str
    resulting_state: str | None


# ── Mock fixtures (deterministic; mirror UI lib/api/mock-handler.ts) ───────


def _mock_signoffs() -> list[SignoffRow]:
    return [
        SignoffRow(
            event_id="signoff-oom-exec",
            parent_incident_key="inc-oom-execution-service",
            timestamp="2026-05-23T11:58:00Z",
            verdict="APPROVED",
            narrative="Layer-0 restart_service succeeded; heap recovered to 38% within 22s.",
            llm_model="claude-sonnet-4-6",
            confidence=0.94,
        ),
        SignoffRow(
            event_id="signoff-dispute",
            parent_incident_key="inc-spurious-killswitch",
            timestamp="2026-05-23T08:03:00Z",
            verdict="DISPUTE_AUTOMATED_ACTION",
            narrative="GLOBAL kill-switch fired on a single-venue blip — disproportionate.",
            llm_model="claude-opus-4-7",
            confidence=0.88,
        ),
    ]


def _mock_audit_ack_queue() -> list[AuditAckRow]:
    now = datetime.now(UTC)
    return [
        AuditAckRow(
            incident_key="inc-oom-execution-service",
            audit_ack_due_at=(now + timedelta(hours=4)).isoformat(),
            severity="HIGH",
            problem_type="OOM_DETECTED",
            service="execution-service",
            llm_verdict="APPROVED",
        ),
        AuditAckRow(
            incident_key="inc-spurious-killswitch",
            audit_ack_due_at=(now - timedelta(minutes=2)).isoformat(),
            severity="CRITICAL",
            problem_type="SPURIOUS_GLOBAL_HALT",
            service="alerting-service",
            llm_verdict="DISPUTE_AUTOMATED_ACTION",
        ),
    ]


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/recovery-audit-signoffs", response_model=list[SignoffRow])
async def get_recovery_audit_signoffs(limit: int = 50) -> list[SignoffRow]:
    """Most recent RecoveryAuditSignoff rows (newest first)."""
    if _cfg.is_mock_mode():
        return _mock_signoffs()[:limit]
    state = get_gateway_state()
    return [
        SignoffRow(
            event_id=s.event_id,
            parent_incident_key=s.parent_incident_key,
            timestamp=s.timestamp.isoformat(),
            verdict=s.verdict.value,
            narrative=s.narrative,
            llm_model=s.llm_model,
            confidence=float(s.confidence) if s.confidence is not None else None,
        )
        for s in state.recent_signoffs(limit=limit)
    ]


@router.get("/audit-ack-queue", response_model=list[AuditAckRow])
async def get_audit_ack_queue(limit: int = 50) -> list[AuditAckRow]:
    """Incidents pending human audit-ack, soonest deadline first."""
    if _cfg.is_mock_mode():
        return _mock_audit_ack_queue()[:limit]
    state = get_gateway_state()
    rows = [
        AuditAckRow(
            incident_key=env.incident_key,
            audit_ack_due_at=env.audit_ack_due_at.isoformat() if env.audit_ack_due_at else "",
            severity=env.severity_hint.value,
            problem_type=env.problem_type,
            service=env.service,
            llm_verdict=state.latest_verdict_for(env.incident_key),
        )
        for env in state.pending_audit_ack()
    ]
    rows.sort(key=lambda r: r.audit_ack_due_at)
    return rows[:limit]


@router.post("/incidents/{incident_key}/operational-ack", response_model=AckResponse)
async def post_operational_ack(incident_key: str, operator_id: str = "operator") -> AckResponse:
    """Operator records the operational-ack (acknowledges they are handling the incident)."""
    if _cfg.is_mock_mode():
        return AckResponse(
            ok=True,
            incident_key=incident_key,
            ack_type="operational",
            acked_at=datetime.now(UTC).isoformat(),
        )
    updated = get_gateway_state().operational_ack(incident_key, operator_id)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown incident_key={incident_key}")
    return AckResponse(
        ok=True,
        incident_key=incident_key,
        ack_type="operational",
        acked_at=(updated.operational_acked_at or datetime.now(UTC)).isoformat(),
    )


@router.post("/incidents/{incident_key}/audit-ack", response_model=AckResponse)
async def post_audit_ack(incident_key: str, operator_id: str = "operator") -> AckResponse:
    """Operator records the audit-ack — stops the SLA countdown + clears the queue entry."""
    if _cfg.is_mock_mode():
        return AckResponse(
            ok=True,
            incident_key=incident_key,
            ack_type="audit",
            acked_at=datetime.now(UTC).isoformat(),
        )
    updated = get_gateway_state().audit_ack(incident_key, operator_id)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown incident_key={incident_key}")
    return AckResponse(
        ok=True,
        incident_key=incident_key,
        ack_type="audit",
        acked_at=(updated.audit_acked_at or datetime.now(UTC)).isoformat(),
    )


@router.post("/incidents", response_model=AckResponse)
async def post_incident(envelope: IncidentEnvelope) -> AckResponse:
    """Ingest an IncidentEnvelope into the gateway (services + game-day injectors emit here).

    Registers the latest snapshot + enqueues for audit-ack if it carries a deadline.
    Mock mode acknowledges without touching the in-memory registry.
    """
    if _cfg.is_mock_mode():
        return AckResponse(
            ok=True,
            incident_key=envelope.incident_key,
            ack_type="registered",
            acked_at=datetime.now(UTC).isoformat(),
        )
    get_gateway_state().register_incident(envelope)
    return AckResponse(
        ok=True,
        incident_key=envelope.incident_key,
        ack_type="registered",
        acked_at=datetime.now(UTC).isoformat(),
    )


@router.post("/signoffs", response_model=SignoffIngestResponse)
async def post_signoff(signoff: RecoveryAuditSignoff) -> SignoffIngestResponse:
    """Ingest a RecoveryAuditSignoff from the LLM recovery-audit agent + drive the
    verdict-mandated gateway reaction (DISPUTE → SAFE_MODE+SEV0; ESCALATE → 1h ack).

    Mock mode records the verdict but takes no state action (no live incident registry).
    """
    if _cfg.is_mock_mode():
        return SignoffIngestResponse(
            ok=True,
            incident_key=signoff.parent_incident_key,
            verdict=signoff.verdict.value,
            resulting_state=None,
        )
    updated = get_gateway_state().process_signoff(signoff)
    return SignoffIngestResponse(
        ok=updated is not None,
        incident_key=signoff.parent_incident_key,
        verdict=signoff.verdict.value,
        resulting_state=updated.state.value if updated is not None else None,
    )
