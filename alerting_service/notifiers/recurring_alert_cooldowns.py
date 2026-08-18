"""Per-event dedup/ladder cooldown windows for recurring DP_*/CONSOLIDATOR_DOWN
alerts.

Split out of ``router.py`` (2026-08-18, Phase 3 of
``unified-trading-pm/plans/active/alerting_service_escalation_ladder_centralization_2026_08_18.md``)
-- ``router.py`` sits at its 1100-line file-size cap; mirrors why
``coalesce.py`` / ``kill_switch_rules.py`` / ``dp_run_mostly_empty_static_backlog.py``
/ ``orchestrator_dispatch*.py`` already exist as siblings. Re-bound under the
original private names in ``router.py`` so the test/patch surface
(``router._RECURRING_ALERT_COOLDOWNS`` / ``router._dedup_window_for``) stays
unchanged.

``orchestrator_dispatch_gate.py`` (Phase 3's escalation ladder gate) imports
:func:`dedup_window_for` directly too -- the SAME per-event cadence table
backs both the top-level alert deduplicator's cooldown AND the ladder's
occurrence window, per the plan's explicit "reuse that table's per-event
cadence rather than inventing a second one" instruction.
"""

from __future__ import annotations

from alerting_service.notifiers.dp_run_mostly_empty_static_backlog import (
    dedup_window_override as _dp_run_mostly_empty_dedup_window_override,
)

# Per-event dedup cooldowns (window >= detector cadence). One key per
# (identity, event) per window; re-nags while down, re-alerts on resolve+recur.
RECURRING_ALERT_COOLDOWNS: dict[str, float] = {
    "DP_CATALOG_NOT_RUNNING": 1800.0,  # 30 min; WARN, ~15 min meta-sweep (census liveness)
    "DP_CRON_DID_NOT_FIRE": 1800.0,  # 30 min; CRITICAL, ~15 min meta-sweep — suppress per-prefix re-fire
    "DP_EVENT_LOOP_STARVED": 1800.0,  # 30 min; WARN, ~5 min sweep cadence
    "DP_RUN_MOSTLY_EMPTY": 1800.0,  # 30 min; CRITICAL, static manifest-cell, >= 900s meta-sweep
    "DP_VM_EXIT_NONZERO": 1800.0,  # 30 min; CRITICAL, static exit-code signal, >= 300s cadence
    "DP_VM_GONE_NO_CAPTURE": 1800.0,  # 30 min; CRITICAL, static exit-code signal, >= 300s cadence
    "DP_VM_PARTIAL_UNCONFIRMED": 1800.0,  # 30 min; WARN, ~5 min sweep cadence
    "DP_VM_PREEMPTED": 1800.0,  # 30 min; INFO, ~5 min sweep — suppress refire per sweep
    "DP_VM_PREEMPTED_NO_RELAUNCH": 1800.0,  # 30 min; CRITICAL, static signal, >= 300s cadence
    "DP_VM_STALL": 1800.0,  # 30 min; WARN, ~5 min sweep cadence
    "DP_SOURCE_RATE_LIMITED": 1800.0,  # 30 min; WARN auto_recover — 429 storms
    "CONSOLIDATOR_DOWN": 3600.0,  # 1h; CRITICAL, once + hourly re-remind while down
    "MANIFEST_CONSOLIDATION_FAILED": 3600.0,  # 1h; WARN->CRITICAL on breaker-open (crash-loop)
    "FEED_REFETCH_FAILED": 3600.0,  # 1h; WARN/HIGH->CRITICAL on breaker-open (same pattern)
}


def dedup_window_for(event_name: str, details: dict[str, object] | None = None) -> float | None:
    """Per-event dedup/ladder window: a cooldown for recurring alerts (WARN
    floods and opted-in static/CRITICAL conditions), else ``None`` (the
    deduplicator's 60s default). DP_RUN_MOSTLY_EMPTY widens to a daily
    cooldown once STATIC BACKLOG fires — see
    ``dp_run_mostly_empty_static_backlog.py``."""
    return _dp_run_mostly_empty_dedup_window_override(event_name, details, RECURRING_ALERT_COOLDOWNS.get(event_name))
