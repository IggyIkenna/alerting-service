"""Data-pipeline alert routing — DP_* family + CONSOLIDATOR_DOWN.

Exposes the routing contract for the data-pipeline self-monitoring alerts
defined in UAC ``DATA_PIPELINE_ALERT_RULES`` (the ~38-mode registry generated
from ``codex/05-infrastructure/data-pipeline-alerts.registry.yaml``). Each
``DataPipelineAlertRule`` carries ``event``, ``severity: AlertSeverity``,
``channels: tuple[AlertChannel, ...]`` and ``escalation`` — emitters (the UTL
``record_empty`` honest-absence gate, the exit_code-aware fleet monitor, the
heartbeat watcher, …) and this router share ONE contract so routing can never
drift from the emitted event.

``data_pipeline_rule_for(event_name)`` is the exact-match lookup the router
uses to decide whether an event is a data-pipeline alert; ``CONSOLIDATOR_DOWN``
is included in the registry (it is already a working data-pipeline alert).
Routing semantics (applied by ``router.route_event``):

  * always mirror to ``#data-pipeline-alerts`` (the SLACK channel) — start
    verbose, drive the count to zero (a persistent alert is a bug to close);
  * ``severity == CRITICAL`` ALSO routes through the existing incident path
    (PagerDuty + Telegram) — reusing the consolidator-rules CRITICAL plumbing,
    NOT forking dedup / ack;
  * INFO/WARN stay channel-only (WARN deduped by the router's
    ``AlertDeduplicator``).

SSOT: ``codex/05-infrastructure/data-pipeline-alerts.md`` +
``plans/active/data_pipeline_hardening_self_monitoring_2026_06_22.md`` Phase 0/2.
"""

from __future__ import annotations

from unified_api_contracts import DATA_PIPELINE_ALERT_RULES, DataPipelineAlertRule

# Exact-match index: event_name → rule. Built once at import from the UAC SSOT
# tuple (closed set, ~38 modes). The DP_* events are matched by NAME (the same
# idiom the sibling rule modules use); CONSOLIDATOR_DOWN is a member.
_RULES_BY_EVENT: dict[str, DataPipelineAlertRule] = {rule.event: rule for rule in DATA_PIPELINE_ALERT_RULES}


def data_pipeline_rule_for(event_name: str) -> DataPipelineAlertRule | None:
    """Return the ``DataPipelineAlertRule`` for ``event_name``, or None.

    Exact-match only (the DP_* family + CONSOLIDATOR_DOWN are explicit event
    names, never globs). A non-data-pipeline event returns None → the router
    does NOT mirror it to ``#data-pipeline-alerts``.
    """
    return _RULES_BY_EVENT.get(event_name)


def is_data_pipeline_event(event_name: str) -> bool:
    """Return True when ``event_name`` is a registered data-pipeline alert."""
    return event_name in _RULES_BY_EVENT
