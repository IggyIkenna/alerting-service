"""STATIC BACKLOG paging-cadence downgrade for ``DP_RUN_MOSTLY_EMPTY``.

Split into its own module rather than grown in ``router.py`` (already at its
1100-line file-size cap) — mirrors why ``coalesce.py`` / ``kill_switch_rules.py``
exist as siblings instead of growing that file further.

Ruling (``plans/active/issues/cefi_high_attempted_failed_batch_cluster_2026_07_23.md``
[BACKEND] P2 todo, 2026-07-28): a ``DP_RUN_MOSTLY_EMPTY`` cell that has sat
unmoving for days re-paging CRITICAL every 30 minutes is the "every tick"
anti-pattern the workspace's own alerting precedent (CLAUDE.md "AO alerts /
Slack notifications" — dedup by state-transition, never every tick) already
rejects elsewhere. Once a cell is labeled STATIC BACKLOG (deployment-service's
``attempted_failed_staleness.py`` stamps ``details["is_static_backlog"]``),
the 30-min CRITICAL re-page is suppressed and downgraded to a WARN-tier,
Slack-only reminder on a much longer (daily) cooldown instead — never full
silence. The instant a cell shows fresh (non-static) activity again, CRITICAL
paging resumes immediately, unsuppressed — both functions here are pure
per-call decisions over the emitted ``details`` payload; neither persists
state, so there is nothing to "reset" when activity resumes.
"""

from __future__ import annotations

from unified_api_contracts import AlertSeverity

# Once STATIC BACKLOG fires for a DP_RUN_MOSTLY_EMPTY cell, its dedup cooldown
# widens from the normal 1800s (30 min) to this daily reminder cadence.
STATIC_BACKLOG_COOLDOWN_SECONDS = 86400.0  # 24h

_DP_RUN_MOSTLY_EMPTY = "DP_RUN_MOSTLY_EMPTY"


def _is_static_backlog(event_name: str, details: dict[str, object] | None) -> bool:
    return event_name == _DP_RUN_MOSTLY_EMPTY and details is not None and details.get("is_static_backlog") is True


def dedup_window_override(
    event_name: str,
    details: dict[str, object] | None,
    default: float | None,
) -> float | None:
    """Widen the dedup cooldown for a STATIC BACKLOG DP_RUN_MOSTLY_EMPTY cell;
    else return the caller's normal per-event ``default`` unchanged.
    ``details=None`` (back-compat for callers that only know the event name)
    always falls through to ``default``."""
    if _is_static_backlog(event_name, details):
        return STATIC_BACKLOG_COOLDOWN_SECONDS
    return default


def effective_severity(
    event_name: str,
    details: dict[str, object],
    severity: AlertSeverity,
) -> AlertSeverity:
    """Downgrade CRITICAL -> WARN for a STATIC BACKLOG DP_RUN_MOSTLY_EMPTY cell.

    WARN is "notify (Telegram only), no page" (see ``AlertSeverity`` docstring)
    and, in the DP_* family specifically, ``router._route_data_pipeline_event``
    only pages PagerDuty/Telegram for CRITICAL — WARN/INFO stay
    channel-mirror-only (the Slack mirror still fires unconditionally, so this
    is a downgrade to a lower-severity periodic reminder, never full silence).
    Every other DP_*/DEPLOYMENT_* event, and a non-CRITICAL DP_RUN_MOSTLY_EMPTY
    fire, is returned unchanged.
    """
    if severity is AlertSeverity.CRITICAL and _is_static_backlog(event_name, details):
        return AlertSeverity.WARN
    return severity
