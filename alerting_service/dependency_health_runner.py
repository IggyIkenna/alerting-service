"""Production wiring for the dependency-health prober — execution-service +
strategy-service.

Constructs the ONLY production ``DependencyHealthProber`` in the fleet today,
scoped to the 2 internal-service dependencies registered 2026-08-22
(``live_path_has_no_stale_producer_revocation_2026_08_14.md`` item 1). The
other 25+ external/cloud-infra dependencies in
``deployment-service/configs/dependency_health_policies.yaml`` stay unprobed
here — wiring THEIR production loading (a cross-repo config-fetch problem
that yaml's own header comment already flags as unsolved, "Status: CONFIG
ONLY — nothing consumes these policies yet") is a separate, pre-existing gap
this change does not attempt to close.

The two policies below are hand-authored, not loaded from that YAML at
runtime: alerting-service and deployment-service are separate deployed
containers, so reading a sibling repo's config file at runtime would require
solving that cross-repo distribution problem first. Kept in sync with the
YAML's own ``execution_service_health`` / ``strategy_service_health`` entries
by convention — comments cross-reference both directions.

Issue: ``/plans/active/issues/live_path_has_no_stale_producer_revocation_2026_08_14.md``
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from unified_api_contracts import KillSwitchScope
from unified_api_contracts.dependency import DependencyClass, DependencyHealthPolicy

from .config import AlertingSystemConfig
from .dependency_health_prober import DependencyHealthProber, ProbeFn

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 5.0
_DEFAULT_INTERVAL_SECONDS = 60

# Kept in sync with deployment-service/configs/dependency_health_policies.yaml
# ("Our own core trading services" section) by convention.
_EXECUTION_SERVICE_HEALTH = DependencyHealthPolicy(
    dependency_id="execution_service_health",
    dependency_class=DependencyClass.INTERNAL_CONTROL_PLANE,
    expected_recovery_time_seconds=120,
    warning_buffer_seconds=60,
    human_investigation_buffer_seconds=900,
    hard_escalation_seconds=900,
    fallback_available=False,
    protected_mode_available=False,
    owner="ikenna@odum-research.com",
    runbook_doc="codex/15-runbooks/incidents/RB-INFRA-001.md",
    test_method="healthcheck_endpoint",
    kill_switch_scope=KillSwitchScope.GLOBAL,
)

_STRATEGY_SERVICE_HEALTH = DependencyHealthPolicy(
    dependency_id="strategy_service_health",
    dependency_class=DependencyClass.INTERNAL_CONTROL_PLANE,
    expected_recovery_time_seconds=120,
    warning_buffer_seconds=60,
    human_investigation_buffer_seconds=900,
    hard_escalation_seconds=900,
    fallback_available=False,
    protected_mode_available=False,
    owner="ikenna@odum-research.com",
    runbook_doc="codex/15-runbooks/incidents/RB-INFRA-001.md",
    test_method="healthcheck_endpoint",
    kill_switch_scope=KillSwitchScope.STRATEGY,
)

INTERNAL_SERVICE_POLICIES: tuple[DependencyHealthPolicy, ...] = (
    _EXECUTION_SERVICE_HEALTH,
    _STRATEGY_SERVICE_HEALTH,
)


def _health_urls(config: AlertingSystemConfig) -> dict[str, str]:
    """Map dependency_id -> configured health-check URL (only entries with a URL set)."""
    urls = {
        "execution_service_health": config.execution_service_health_url,
        "strategy_service_health": config.strategy_service_health_url,
    }
    return {dep_id: url for dep_id, url in urls.items() if url}


def make_probe_fn(config: AlertingSystemConfig) -> ProbeFn:
    """Build the real ``probe_fn`` for ``DependencyHealthProber``.

    Fails OPEN (reports healthy) for any dependency without a configured URL —
    the same default posture as every scaffolded probe in the fleet today
    (``dependency_health_prober._dispatch``'s own module docstring). This lets
    the wiring ship + run safely before the operator populates the two URL
    config fields; populating them activates real probing with no further
    code change.
    """
    urls = _health_urls(config)

    async def _probe(policy: DependencyHealthPolicy) -> bool:
        url = urls.get(policy.dependency_id)
        if not url:
            return True
        try:
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
                resp = await client.get(url)
            return resp.status_code == 200
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.warning(
                "dependency health probe failed: dependency_id=%s error=%s",
                policy.dependency_id,
                repr(exc)[:200],
            )
            return False

    return _probe


async def run_dependency_health_probing(
    config: AlertingSystemConfig,
    *,
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Background loop: probe execution-service + strategy-service health forever.

    Mirrors ``gateway/provider_health_probe.py``'s ``run_forever`` shape.
    Cancel via ``task.cancel()`` at shutdown. A probe-iteration exception never
    kills the loop — logged, and the next tick proceeds, same posture as the
    provider-health probe.
    """
    prober = DependencyHealthProber(list(INTERNAL_SERVICE_POLICIES), probe_fn=make_probe_fn(config))
    while True:
        try:
            await prober.probe_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Dependency health probing iteration failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


__all__ = [
    "INTERNAL_SERVICE_POLICIES",
    "make_probe_fn",
    "run_dependency_health_probing",
]
