"""GitHub ``repository_dispatch`` delivery — the orchestrator fast-spawn HTTP
call, relocated from deployment-service (Phase 2 of
``unified-trading-pm/plans/active/alerting_service_escalation_ladder_centralization_2026_08_18.md``,
"Centralize disaster-recovery escalation-ladder decisions in
alerting-service"). Sibling to ``pagerduty.py`` / ``uts_live_alerts_slack.py``
/ ``data_pipeline_slack.py``.

Ported from ``deployment-service/deployment_service/data_pipeline_monitors/
escalation.py::_dispatch_to_orchestrator()`` -- same ``GH_PAT``
Secret-Manager fetch, same ``IggyIkenna/unified-trading-pm`` ``dispatches``
POST, same ``escalate-to-orchestrator``/``data_pipeline_failure`` payload
shape, same ``_VM_LIFECYCLE_EVENTS``-based relaunch-context text (gated on
``orchestrator_dispatch_budget.check_relaunch_dispatch_budget``, ported
alongside it in the sibling module). Reads its inputs from the
``event_name``/``details`` dict ``alerting_service/notifiers/
router.py::_route_data_pipeline_event`` already receives -- ``registry_id``,
``vm_name``, ``relaunch_launcher``, ``deployment_id``, ``asset_group``,
``severity``, ``filed_issue`` all ride in ``details`` today via
deployment-service's ``route_finding()`` stamping, so no new field was added
to the emitted event for this phase.

Deviation from the original -- cached secret fetch
------------------------------------------------------
The ``GH_PAT`` secret fetch here is cached for the process lifetime (a
capability-probe, mirroring ``pagerduty.py``'s ``_probe_routing_key``)
rather than fetched fresh on every call like the deployment-service
original. The original deliberately does NOT cache because
deployment-service's Cloud Run JOB gets a fresh container per execution
anyway (a process-lifetime cache would never hit there); alerting-service is
a Cloud Run SERVICE (long-lived, warm), where the uncached form would hit
Secret Manager on every single escalation-tier finding -- the exact "hammers
Secret Manager on every alert" failure mode ``pagerduty.py``'s own docstring
documents fixing (2026-08-06, PagerDuty routing-key probe). Functionally
equivalent (the ``dispatched: bool`` outcome is unchanged); only the
secret-fetch caching strategy differs.

Best-effort throughout -- every failure mode (no ``GH_PAT``, Secret Manager
error, network error, non-2xx GitHub response) returns a structured
``{"dispatched": False, "reason": ...}`` and NEVER raises: a dispatch
failure must not break the alert route (mirrors every other notifier in this
package).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from functools import lru_cache
from http.client import HTTPResponse
from typing import cast

from unified_trading_library import UnifiedCloudConfig, get_secret_client, log_event

from .orchestrator_dispatch_budget import check_relaunch_dispatch_budget

logger = logging.getLogger(__name__)

# The PM repo + repository_dispatch event-type that fast-spawns an autonomous
# worker for a data-pipeline wall (the same fast path CI failures use).
_DISPATCH_PM_REPO = "IggyIkenna/unified-trading-pm"
_DISPATCH_EVENT_TYPE = "escalate-to-orchestrator"
_DISPATCH_WALL_TYPE = "data_pipeline_failure"
# Secret-Manager name of the workflow-capable GitHub PAT (carries repo +
# workflow scope) -- the SAME secret deployment-service's original read.
_GH_PAT_SECRET = "GH_PAT"

# A misclassified-empty / not-v9 / divergence finding -> the per-AG MTDS/IS
# repo; a VM-lifecycle finding (stall / exit / starvation) -> deployment-service.
# Mirrors escalation.py's own constant (a local-string-constant pattern,
# DP_VM_PREEMPTED / DP_VM_PARTIAL_UNCONFIRMED are deliberately NOT UTL-exported
# constants there either -- this fix is scoped to the escalation-dispatch path).
_VM_LIFECYCLE_EVENTS = frozenset(
    {
        "DP_VM_STALL",
        "DP_VM_EXIT_NONZERO",
        "DP_EVENT_LOOP_STARVED",
        "CONSOLIDATOR_DOWN",
        "DP_VM_PREEMPTED",
        "DP_VM_PARTIAL_UNCONFIRMED",
    }
)


@lru_cache(maxsize=1)
def _get_cloud_config() -> UnifiedCloudConfig:
    """Return singleton UnifiedCloudConfig instance (mirrors pagerduty.py)."""
    return UnifiedCloudConfig()


@lru_cache(maxsize=1)
def _probe_gh_pat(project_id: str) -> str | None:
    """Probe Secret Manager ONCE per process for the workflow-capable
    ``GH_PAT`` (capability-probe pattern -- see module docstring's
    "Deviation from the original" section). Returns ``None`` (logged once)
    on a missing secret or any Secret Manager failure -- never raises.
    """
    try:
        token = get_secret_client(project_id=project_id).get_secret(_GH_PAT_SECRET)
    except Exception as exc:  # noqa: broad-except — capability probe: SM errors must never propagate
        logger.info(
            "dispatch: GH_PAT probe failed (falling through to the file_issue path): %s",
            exc,
        )
        return None
    if not token:
        logger.info("dispatch: GH_PAT secret not found -- fast-spawn dispatch marked unavailable")
        return None
    return token


def _target_repo_for(event_name: str) -> str:
    """Name the repo a worker should fix the finding in (for the actionable
    context). Mirrors deployment-service's ``escalation_issue_writer.
    target_repo_for`` exactly: a VM-lifecycle finding (stall / exit /
    starvation / consolidator) -> the launcher/monitor home
    ``deployment-service``; everything else (misclassified-empty / not-v9 /
    divergence) -> ``market-tick-data-service`` (MTDS owns the capture).
    """
    return "deployment-service" if event_name in _VM_LIFECYCLE_EVENTS else "market-tick-data-service"


def dispatch_to_orchestrator(event_name: str, summary: str, details: dict[str, object]) -> dict[str, object]:
    """Best-effort ``repository_dispatch`` -> ``escalate-to-orchestrator``
    (the CI fast path), fired for a FILE_ISSUE/PAGE_OPERATOR-tier
    data-pipeline finding.

    Fires the SAME fast-spawn path CI failures use: a ``repository_dispatch``
    to the PM repo with ``client_payload[wall_type]=data_pipeline_failure``,
    which the ``escalate-to-orchestrator.yml`` workflow routes to AutoSpawn
    -> an autonomous worker.

    Args:
        event_name: the DP_*/CONSOLIDATOR_DOWN event constant.
        summary: the one-line human summary (``[<event>] <message>`` shape,
            as built by ``router.py``'s ``_route_data_pipeline_event``).
        details: the event's details dict -- reads ``registry_id``,
            ``severity``, ``vm_name``, ``relaunch_launcher``,
            ``deployment_id``, ``asset_group``, ``filed_issue``.

    Returns:
        ``{"dispatched": bool, "reason": str, "http_status": int | None}``.
        Never raises -- every failure mode (no GH token, Secret Manager
        error, network error, non-2xx) degrades to
        ``{"dispatched": False, "reason": ...}``.
    """
    config = _get_cloud_config()
    token = _probe_gh_pat(config.gcp_project_id)
    if not token:
        return {"dispatched": False, "reason": "no_gh_token"}

    registry_id = str(details.get("registry_id", "")).strip()  # noqa: qg-empty-fallback — absent means no registry id
    severity = str(details.get("severity", "")).strip()  # noqa: qg-empty-fallback — absent falls back to UNKNOWN below
    vm_name = str(details.get("vm_name", "")).strip()  # noqa: qg-empty-fallback — absent means no VM-lifecycle identity
    relaunch_launcher = str(details.get("relaunch_launcher", "")).strip()  # noqa: qg-empty-fallback — absent means resolve via launcher_registry (context text below)
    deployment_id = str(details.get("deployment_id", "")).strip()  # noqa: qg-empty-fallback — absent means the finding carries no deployment identity
    asset_group = str(details.get("asset_group", "")).strip()  # noqa: qg-empty-fallback — absent means a non-asset_group-scoped finding
    filed_issue = str(details.get("filed_issue", "")).strip()  # noqa: qg-empty-fallback — absent means no issue was filed
    is_relaunch = event_name in _VM_LIFECYCLE_EVENTS

    # Machine-enforced <=2/(launcher-family,shard-year,day) relaunch-DISPATCH
    # bound (see orchestrator_dispatch_budget.check_relaunch_dispatch_budget's
    # docstring) -- a bounded launcher-family gets told NOT to relaunch
    # instead of a bare "RELAUNCH vm=..." instruction.
    relaunch_budget = check_relaunch_dispatch_budget(vm_name=vm_name) if is_relaunch else None
    if is_relaunch and relaunch_budget is not None and relaunch_budget.get("bounded"):
        relaunch_ctx = (
            f" DO NOT RELAUNCH vm={vm_name or '?'} — group "
            f"{relaunch_budget.get('shard_key') or relaunch_budget['vm_prefix']} already hit "
            f"{relaunch_budget['dispatches_today']}/{relaunch_budget['max_per_day']} relaunch dispatches today "
            "(RB-INFRA-RELAUNCH bound). Check for an existing open issue doc and page the operator instead of "
            "relaunching again. Runbook: codex/15-runbooks/incidents/rb_infra_relaunch.md."
        )
    else:
        relaunch_ctx = (
            f" RELAUNCH vm={vm_name or '?'} launcher={relaunch_launcher or '(resolve via launcher_registry)'} "
            f"deployment_id={deployment_id or '?'} asset_group={asset_group or '?'}. "
            "Runbook: codex/15-runbooks/incidents/rb_infra_relaunch.md."
            if is_relaunch
            else ""
        )
    context = (
        f"{severity or 'UNKNOWN'} {event_name} ({registry_id or 'n/a'}) — {summary}. "
        f"Filed issue: {filed_issue or '(none — alert carries the details)'}."
        f"{relaunch_ctx} "
        "Read SUB_AGENT_MANDATORY_RULES.md + codex/05-infrastructure/data-pipeline-alerts.md + the filed issue."
    )
    target_repo = _target_repo_for(event_name)
    payload = {
        "event_type": _DISPATCH_EVENT_TYPE,
        "client_payload": {
            "repo": target_repo,
            "pr_number": "0",
            "wall_type": _DISPATCH_WALL_TYPE,
            "context": context,
            "authoring_slot": "dp-fleet-monitor",
            "model": "sonnet",
        },
    }
    request = urllib.request.Request(
        f"https://api.github.com/repos/{_DISPATCH_PM_REPO}/dispatches",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "dp-fleet-monitor",
        },
    )
    try:
        # nosec B310: the URL host is the fixed GitHub API (https://api.github.com),
        # never a user-controlled / file:/ scheme -- the repo path is a module constant.
        resp = cast("HTTPResponse", urllib.request.urlopen(request, timeout=15))  # nosec B310
        try:
            status: int = resp.status
        finally:
            resp.close()
        dispatched = 200 <= status < 300
        if dispatched:
            log_event(
                "ORCHESTRATOR_DISPATCH_SENT",
                details={"event_name": event_name, "registry_id": registry_id, "repo": target_repo},
            )
        return {"dispatched": dispatched, "reason": f"http_{status}", "http_status": status}
    except urllib.error.HTTPError as exc:
        logger.warning("dispatch: repository_dispatch HTTP %s (best-effort): %s", exc.code, exc)
        return {"dispatched": False, "reason": f"http_{exc.code}", "http_status": exc.code}
    except Exception as exc:  # noqa: broad-except — network/timeout: dispatch failure must never break the finding
        logger.warning("dispatch: repository_dispatch failed (best-effort): %s", exc)
        return {"dispatched": False, "reason": f"error: {exc!r}"[:200]}
