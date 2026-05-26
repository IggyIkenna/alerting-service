"""Incident Gateway — central state machine + dedup + audit-ack queue.

Tier-2 of the 5+1 defence-in-depth recovery model (codex/04-architecture/
recovery-defence-in-depth-layers.md). Subscribes to AgentActionEvent + emits
IncidentEnvelope snapshots at each state transition + holds the audit-ack
queue.

Codex SSOT: ``codex/04-architecture/incident-gateway-state-machine.md``.
Implementation plan: ``plans/active/incident_gateway_and_state_machine_2026_05_23.md``.

This package contains scaffolds — the router refactor + provider-health-probe
land in a follow-up commit per pair-review with Harsh.
"""

from __future__ import annotations

from alerting_service.gateway.audit_ack_queue import (
    AuditAckQueue,
    AuditAckQueueEntry,
)
from alerting_service.gateway.dedup import compute_incident_key
from alerting_service.gateway.envelope_adapter import wrap_legacy_alert
from alerting_service.gateway.evidence_collector import (
    EvidenceCollector,
    EvidenceCollectorConfig,
)
from alerting_service.gateway.provider_health_probe import (
    ProbeConfig,
    ProbeResult,
    ProviderHealthProbe,
)
from alerting_service.gateway.state_machine import IncidentStateMachine

__all__ = [
    "AuditAckQueue",
    "AuditAckQueueEntry",
    "EvidenceCollector",
    "EvidenceCollectorConfig",
    "IncidentStateMachine",
    "ProbeConfig",
    "ProbeResult",
    "ProviderHealthProbe",
    "compute_incident_key",
    "wrap_legacy_alert",
]
