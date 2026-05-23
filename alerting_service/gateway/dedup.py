"""Incident dedup-key — stable hash for collapsing N retries into 1 incident.

The same root cause across N detector firings within a 5-minute window =
1 IncidentEnvelope with N AgentActionEvent children, NOT N IncidentEnvelopes.

Codex SSOT: ``codex/04-architecture/incident-gateway-state-machine.md``
§ "Dedup-key ``incident_key``".
"""

from __future__ import annotations

import hashlib


def compute_incident_key(
    *,
    service: str,
    component: str,
    problem_type: str,
    strategy_id: str | None = None,
    venue: str | None = None,
    instrument_id: str | None = None,
) -> str:
    """Compute a stable incident_key from the scope tuple.

    Same scope tuple = same key. Collisions across distinct incidents are
    expected (and desired) within the 5-minute dedup window — the gateway
    expires old keys via the audit-ack queue rather than via key uniqueness.

    The hash uses SHA-256 truncated to 16 hex chars (64 bits) — collision
    probability is negligible at our incident volume (10k/year sustained).

    Examples::

        >>> compute_incident_key(
        ...     service="execution-service",
        ...     component="order-router",
        ...     problem_type="OOM_DETECTED",
        ... )
        'inc-<16-hex>'
    """
    canonical_tuple = (
        service.strip().lower(),
        component.strip().lower(),
        problem_type.strip().upper(),
        (strategy_id or "_").strip().lower(),
        (venue or "_").strip().lower(),
        (instrument_id or "_").strip().lower(),
    )
    payload = "|".join(canonical_tuple).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"inc-{digest}"
