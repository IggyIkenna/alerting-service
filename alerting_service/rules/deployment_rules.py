"""Deployment lifecycle alert routing — DEPLOYMENT_STARTED/COMPLETED/FAILED.

Deployment lifecycle events (the VM / Cloud-Run STARTED / COMPLETED / FAILED
family emitted via UTL ``ServiceBootstrap`` + the deployment-service launchers)
reach Slack at /repos grade: the ``#data-pipeline-alerts`` channel mirror with
the deployment **umbrella** + cloud + a ``/deployments/{name}`` deep-link.

This mirrors ``rules/data_pipeline_rules.py`` for the deployment family. The
event NAMES are the UTL lifecycle constants (``DEPLOYMENT_STARTED`` /
``DEPLOYMENT_COMPLETED`` / ``DEPLOYMENT_FAILED``); a deployment FAILURE routes
CRITICAL (so it ALSO pages via the existing incident path), START/COMPLETE are
INFO channel-only.

Routing semantics (applied by ``router.route_event`` via the shared
``_route_data_pipeline_event`` path — same dedup / persistence / Slack mirror,
never forked): always mirror to ``#data-pipeline-alerts`` with the umbrella +
deep-link; ``severity == CRITICAL`` ALSO pages (PagerDuty + Telegram).

SSOT: ``plans/active/deployment_observability_parity_live_batch_paper_2026_06_22.md``
Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass

from unified_api_contracts import AlertSeverity
from unified_trading_library import (
    DEPLOYMENT_COMPLETED,
    DEPLOYMENT_FAILED,
    DEPLOYMENT_STARTED,
)


@dataclass(frozen=True)
class DeploymentAlertRule:
    """Routing contract for a deployment lifecycle event.

    ``event`` is the UTL lifecycle constant; ``severity`` drives the channel /
    paging behaviour (CRITICAL pages, INFO is channel-only). Shape-compatible
    with ``DataPipelineAlertRule`` for ``router._route_data_pipeline_event``
    (only ``.severity`` is read on the routing path).
    """

    event: str
    severity: AlertSeverity


# Exact-match index: a deployment FAILURE pages (CRITICAL); START / COMPLETE are
# informational channel mirrors so the operator sees the lifecycle in Slack.
_RULES_BY_EVENT: dict[str, DeploymentAlertRule] = {
    DEPLOYMENT_STARTED: DeploymentAlertRule(DEPLOYMENT_STARTED, AlertSeverity.INFO),
    DEPLOYMENT_COMPLETED: DeploymentAlertRule(DEPLOYMENT_COMPLETED, AlertSeverity.INFO),
    DEPLOYMENT_FAILED: DeploymentAlertRule(DEPLOYMENT_FAILED, AlertSeverity.CRITICAL),
}


def deployment_rule_for(event_name: str) -> DeploymentAlertRule | None:
    """Return the ``DeploymentAlertRule`` for ``event_name``, or None.

    Exact-match only (the deployment lifecycle constants are explicit event
    names, never globs). A non-deployment event returns None.
    """
    return _RULES_BY_EVENT.get(event_name)


def is_deployment_event(event_name: str) -> bool:
    """Return True when ``event_name`` is a registered deployment lifecycle alert."""
    return event_name in _RULES_BY_EVENT
