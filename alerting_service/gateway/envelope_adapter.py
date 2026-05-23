"""Backward-compatible wrapper for legacy alert payloads → IncidentEnvelope.

The existing router consumes `dict` payloads (or `DefiAlert` Pydantic models)
emitted by services that pre-date the Tier-1 UAC schemas. Rather than force a
big-bang refactor of every emitter, this adapter wraps legacy payloads into
fresh IncidentEnvelope instances so the new gateway state machine + audit-ack
queue + dedup-key work end-to-end immediately.

Per CLAUDE.md "alerting-service is Harsh's repo" — this is the minimal-blast-
radius bridge that keeps the existing router working unchanged while adding
the new state machine on top. Full router refactor (consuming IncidentEnvelope
directly) lands in a follow-up pair-review with Harsh.

Codex SSOT: ``codex/04-architecture/incident-gateway-state-machine.md``
§ "IncidentEnvelope schema".
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import BaseModel
from unified_api_contracts.alerting import AlertSeverity
from unified_api_contracts.incident import IncidentEnvelope, IncidentState
from unified_trading_library import UnifiedCloudConfig

from alerting_service.gateway.dedup import compute_incident_key

_logger = logging.getLogger(__name__)
_cfg = UnifiedCloudConfig()


def wrap_legacy_alert(
    payload: dict[str, object] | BaseModel,
    *,
    fallback_service: str = "unknown",
    fallback_environment: str | None = None,
    code_version: str | None = None,
    config_hash: str | None = None,
) -> IncidentEnvelope:
    """Wrap a legacy alert payload (raw dict or DefiAlert Pydantic) into an
    IncidentEnvelope.

    The function is intentionally permissive — every IncidentEnvelope field
    has a sensible default if the legacy payload omits it.

    Args:
        payload: Legacy alert (dict-shaped OR a Pydantic instance with
            ``.model_dump()`` available, e.g. ``DefiAlert``).
        fallback_service: Used if ``payload["service"]`` is absent.
        fallback_environment: Defaults to env var ``ENVIRONMENT`` or ``"prod"``.
        code_version: Stamped on envelope. Best-effort from caller.
        config_hash: Stamped on envelope. Best-effort from caller.
    """
    # Normalise to dict
    if isinstance(payload, BaseModel):
        body: dict[str, object] = dict(payload.model_dump())
    else:
        body = dict(payload)

    # Closed-set environment fallback
    env = body.get("environment") or fallback_environment or _cfg.environment or "prod"
    if env not in ("prod", "staging", "dev"):
        env = "prod"

    # Severity hint — accept either AlertSeverity or legacy strings
    sev_raw = body.get("severity") or body.get("severity_hint")
    severity_hint = _normalize_severity(sev_raw)

    # Required fields with defaults
    service = body.get("service") or fallback_service
    component = body.get("component") or "legacy"
    problem_type = (
        body.get("event") or body.get("event_name") or body.get("problem_type") or "LEGACY_ALERT"
    )
    problem_summary = body.get("message") or body.get("problem_summary") or str(problem_type)

    # Optional scope fields
    strategy_id = body.get("strategy_id")
    strategy_family = body.get("strategy_family")
    venue = body.get("venue")
    account_id = body.get("account_id")
    instrument_id = body.get("instrument_id") or body.get("symbol")

    incident_key = compute_incident_key(
        service=str(service),
        component=str(component),
        problem_type=str(problem_type),
        strategy_id=str(strategy_id) if strategy_id else None,
        venue=str(venue) if venue else None,
        instrument_id=str(instrument_id) if instrument_id else None,
    )

    return IncidentEnvelope(
        event_id=str(body.get("event_id") or incident_key),
        incident_key=incident_key,
        timestamp=datetime.now(UTC),
        state=IncidentState.DETECTED,
        environment=str(env),
        severity_hint=severity_hint,
        domain=str(body.get("domain") or "live_trading"),
        service=str(service),
        component=str(component),
        strategy_id=str(strategy_id) if strategy_id else None,
        strategy_family=str(strategy_family) if strategy_family else None,
        venue=str(venue) if venue else None,
        account_id=str(account_id) if account_id else None,
        instrument_id=str(instrument_id) if instrument_id else None,
        problem_type=str(problem_type).upper(),
        problem_summary=str(problem_summary),
        config_hash=config_hash or "unknown",
        code_version=code_version or "unknown",
    )


def _normalize_severity(value: object) -> AlertSeverity:
    """Map legacy severity strings (critical / warning / info / etc) to
    AlertSeverity. Unknown → WARN (safe default)."""
    if value is None:
        return AlertSeverity.WARN
    if isinstance(value, AlertSeverity):
        return value
    s = str(value).strip().upper()
    # Direct enum-value match
    try:
        return AlertSeverity(s)
    except ValueError:
        pass
    # Legacy strings
    mapping = {
        "CRITICAL": AlertSeverity.CRITICAL,
        "ERROR": AlertSeverity.CRITICAL,
        "P0": AlertSeverity.CRITICAL,
        "SEV0": AlertSeverity.CRITICAL,
        "HIGH": AlertSeverity.HIGH,
        "P1": AlertSeverity.HIGH,
        "SEV1": AlertSeverity.HIGH,
        "WARNING": AlertSeverity.WARN,
        "WARN": AlertSeverity.WARN,
        "P2": AlertSeverity.WARN,
        "SEV2": AlertSeverity.WARN,
        "INFO": AlertSeverity.INFO,
        "P3": AlertSeverity.INFO,
        "SEV3": AlertSeverity.INFO,
    }
    return mapping.get(s, AlertSeverity.WARN)
