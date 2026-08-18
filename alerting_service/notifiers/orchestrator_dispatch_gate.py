"""Escalation-tier gate for the relocated orchestrator fast-spawn dispatch.

Phase 2 of
``unified-trading-pm/plans/active/alerting_service_escalation_ladder_centralization_2026_08_18.md``.
Split out of ``router.py`` (2026-08-18) -- ``router.py`` sits at its
1100-line file-size cap; mirrors why ``coalesce.py`` / ``kill_switch_rules.py``
/ ``dp_run_mostly_empty_static_backlog.py`` already exist as siblings.
Re-bound under the original private name (``_maybe_dispatch_to_orchestrator``)
in ``router.py`` so the test/patch surface stays unchanged.

Phase 3 (same plan) adds the GCS-durable escalation ladder
(``alerting_service.escalation_ladder``) as a gate BEFORE the dedup check +
dispatch call below: a FILE_ISSUE/PAGE_OPERATOR-tier finding must cross the
ladder's occurrence threshold (CLOSED->OPEN, or a HALF_OPEN->OPEN
re-escalation after a cooldown) before the loud GitHub-dispatch path fires
at all -- replacing the old "dispatch on every tier-matched, non-deduped
occurrence" behavior with "try N occurrences quietly [the per-event Slack
mirror already fires unconditionally], the Nth crossing escalates".
"""

from __future__ import annotations

import logging

from unified_api_contracts.alerting import derive_escalation_identity

from ..escalation_ladder import record_occurrence
from .orchestrator_dispatch import dispatch_to_orchestrator
from .orchestrator_dispatch_budget import check_dispatch_dedup_gcs
from .recurring_alert_cooldowns import dedup_window_for

logger = logging.getLogger(__name__)

# Escalation tiers (deployment-service's EscalationTier.FILE_ISSUE /
# .PAGE_OPERATOR values, stamped onto every DP_* event's details as
# "escalation_tier" by route_finding()) that gate the relocated
# orchestrator-dispatch call below. AUTO_RECOVER findings that fell through
# to FILE_ISSUE also carry "escalation_tier": "file_issue" at that point
# (route_finding sets effective_tier before stamping), so they are covered
# too -- mirrors the original's `should_dispatch` PAGE_OPERATOR-or-FILE_ISSUE
# shape without needing deployment-service's own filed_issue_path/
# actuator_needs_worker bookkeeping (Phase 2's third todo removes the
# dispatch call + that bookkeeping from escalation.py entirely, so this
# router-side gate becomes the ONLY dispatch trigger).
_DISPATCH_GATED_TIERS = frozenset({"file_issue", "page_operator"})

# Fallback ladder window for a tier-gated event absent from
# `recurring_alert_cooldowns.RECURRING_ALERT_COOLDOWNS` (e.g. DEPLOYMENT_FAILED,
# which also routes through `_route_data_pipeline_event`/this gate but isn't a
# DP_* recurring-alert code) -- the most common entry in that table, so an
# unlisted event gets the same 30-minute occurrence window as the majority of
# DP_* findings rather than an arbitrary bespoke value.
_DEFAULT_LADDER_WINDOW_SECONDS = 1800.0


def _resolve_ladder_identity(details: dict[str, object]) -> str | None:
    """Best-effort escalation identity for the ladder check -- ``None`` when
    the finding carries neither identity shape
    (:func:`unified_api_contracts.alerting.derive_escalation_identity`
    raises ``ValueError`` in that case; a finding this gate can't identify
    can't be laddered, so it falls through to the dedup+dispatch path
    unchanged, exactly like `record_occurrence`'s own `None` fail-open
    contract)."""
    registry_id = str(details.get("registry_id", ""))  # noqa: qg-empty-fallback — absent means no registry id
    asset_group = str(details.get("asset_group_name", "")).strip()  # noqa: qg-empty-fallback — absent means not tuple-shaped
    data_type = str(details.get("data_type", "")).strip()  # noqa: qg-empty-fallback — same as asset_group_name above
    vm_name = str(details.get("vm_name", "")).strip()  # noqa: qg-empty-fallback — absent means not vm-shaped
    try:
        return derive_escalation_identity(
            registry_id=registry_id, vm_name=vm_name, asset_group=asset_group, data_type=data_type
        )
    except ValueError:
        return None


def maybe_dispatch_to_orchestrator(event_name: str, summary: str, details: dict[str, object]) -> None:
    """Gate + fire the relocated GitHub ``repository_dispatch`` fast-spawn
    call for a FILE_ISSUE/PAGE_OPERATOR-tier data-pipeline finding.

    Phase 3's escalation ladder (``alerting_service.escalation_ladder.
    record_occurrence``) runs FIRST, before the dedup check below: a finding
    below the ladder's occurrence threshold (or already OPEN and still
    within its cooldown) is muted here -- returns without dispatching, and
    without even consulting the dedup checkpoint. Only a genuine CLOSED->OPEN
    (or cooldown-elapsed HALF_OPEN->OPEN) crossing -- or a ladder read/write
    failure, which fails OPEN toward dispatching rather than silently
    swallowing a PAGE_OPERATOR-tier finding -- proceeds to the dedup +
    dispatch logic that already existed here.

    Applies the GCS-durable dispatch-dedup checkpoint
    (``orchestrator_dispatch_budget.check_dispatch_dedup_gcs``) when the
    finding carries the ``(asset_group_name, data_type)`` tuple shape
    (``DP_RUN_MOSTLY_EMPTY``/``DP-FETCH-009``-style findings) -- mirrors
    deployment-service's ``check_dispatch_dedup_for_finding``'s tuple-shape
    precedence. alerting-service has no local PM clone, so (unlike
    deployment-service) there is no vm_name-keyed open-issue-doc dedup
    ported here; a VM-lifecycle finding's relaunch volume is still bounded
    by ``dispatch_to_orchestrator``'s own relaunch-dispatch budget check.

    Best-effort, never raises -- mirrors every other notifier dispatch in
    this router (a dispatch failure must not break alert delivery).
    """
    tier = str(details.get("escalation_tier", ""))  # noqa: qg-empty-fallback — absent means no escalation tier at all
    if tier not in _DISPATCH_GATED_TIERS:
        return
    try:
        identity = _resolve_ladder_identity(details)
        if identity is not None:
            window = dedup_window_for(event_name, details) or _DEFAULT_LADDER_WINDOW_SECONDS
            transition = record_occurrence(identity, window_seconds=window)
            if transition == "":
                return  # below the escalation threshold, or already-OPEN-and-quiet -- muted
            # transition is STATE_OPEN (a genuine crossing/re-crossing) or
            # None (ladder state unresolvable) -- both fall through to the
            # dedup+dispatch logic below; see record_occurrence's docstring
            # for why None must never be treated like "".
        asset_group = str(details.get("asset_group_name", "")).strip()  # noqa: qg-empty-fallback — absent means this finding isn't tuple-shaped
        data_type = str(details.get("data_type", "")).strip()  # noqa: qg-empty-fallback — same as asset_group_name above
        if asset_group and data_type:
            dedup = check_dispatch_dedup_gcs(
                asset_group=asset_group,
                data_type=data_type,
                registry_id=str(details.get("registry_id", "")),  # noqa: qg-empty-fallback — absent means no registry id
                max_attempted_at=str(details.get("max_attempted_at", "")),  # noqa: qg-empty-fallback — no recorded attempt timestamp legitimately dedups as "no prior attempt"
                event=event_name,
                is_static_backlog=bool(details.get("is_static_backlog", False)),
            )
            if dedup is not None and dedup.get("skipped"):
                return
        dispatch_to_orchestrator(event_name, summary, details)
    except Exception as exc:  # noqa: broad-except — dispatch gating must never break the alert route
        logger.warning("orchestrator dispatch gate failed (best-effort, no dispatch): %s", exc)
