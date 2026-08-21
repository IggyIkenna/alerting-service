"""Active feed self-healing — recovery decision tree for a stale critical feed.

Phase 2 of
``plans/active/data_feed_sla_registry_and_active_self_healing_2026_06_19.md``.

Wires the Phase-1 freshness registry to the EXISTING recovery machinery (does
NOT rebuild any of it). When a ``critical`` data feed goes stale:

(a) the order-blocking ``freshness_gate`` ALREADY blocks new orders — no change
    here (this module never touches the gate; it stays closed until the feed
    recovers, which is the freshness_gate's own job).
(b) fire the feed's BOUND Layer-0 action (Blue Flame's SILENT_RETRY) — read
    from ``ALL_FRESHNESS_CONTRACTS[feed_id].refetch_action``: ``refetch-feed:``
    (deployment-service ``refetch_feed.py`` REST re-pull) or
    ``rotate-websocket:`` (``rotate_websocket.py`` ws-session rotation for
    ws-sourced venues — a re-pull cannot revive a dead socket).
(c) on REPEATED refetch failure escalate WARN → CRITICAL through the existing
    ``AlertSeverity`` ladder + the audit-ack SLA (``lookup_sla``), routed via
    ``route_event_with_explicit_channels`` — mirroring ``consolidator_rules``'
    repeat-failure escalation idiom (the same ``CircuitBreaker`` machinery,
    keyed per feed).
(d) SUSTAINED failure (breaker OPEN) attaches an advisory position-reduction
    recommendation — the EXISTING drawdown/risk advisory shape from
    ``risk_threshold_rules`` (a structured alert record), NOT a new actuator.

Escalation cadence is mapped to ``criticality``: a ``critical`` feed uses the
tight CRITICAL audit-ack SLA; an ``important`` feed the looser HIGH SLA.

Service event taxonomy:
  SERVICE_EVENT: FEED_REFETCH_ALERT_ROUTED
"""

from __future__ import annotations

import logging

from unified_api_contracts import AlertSeverity
from unified_api_contracts.incident import lookup_sla
from unified_api_contracts.internal import ALL_FRESHNESS_CONTRACTS
from unified_trading_library import log_event

from ..circuit_breaker import STATE_OPEN, CircuitBreaker
from ..notifiers.pagerduty import PagerDutySeverity
from ..notifiers.router import route_event_with_explicit_channels

logger = logging.getLogger(__name__)

# Event names this module emits (matched by NAME like the sibling rule modules).
FEED_REFETCH_TRIGGERED = "FEED_REFETCH_TRIGGERED"
FEED_REFETCH_FAILED = "FEED_REFETCH_FAILED"

# The legacy/fallback Layer-0 action id prefix (DataFreshnessContract.refetch_action).
_REFETCH_ACTION_PREFIX = "refetch-feed"


def _resolve_bound_action(feed_id: str) -> str:
    """The feed's bound Layer-0 recovery action id from the freshness registry —
    ``refetch-feed:<source>`` (REST re-pull) or ``rotate-websocket:<source>``
    (ws-session rotation). Unknown/unbound feeds fall back to the legacy
    refetch id so the escalation record still names a concrete verb.
    """
    contract = ALL_FRESHNESS_CONTRACTS.get(feed_id)
    if contract is not None and contract.refetch_action is not None:
        return contract.refetch_action
    return f"{_REFETCH_ACTION_PREFIX}:{feed_id}"


_PAGING_CHANNELS: frozenset[str] = frozenset({"pagerduty", "telegram"})
_CRITICAL: PagerDutySeverity = "critical"
_WARNING: PagerDutySeverity = "warning"

# Repeat-failure escalation breaker (reuses the service CircuitBreaker idiom —
# do NOT invent new machinery; same as consolidator_rules). A single refetch
# failure is usually healed by the next attempt; 3 failures inside a 30-minute
# sliding window keyed on the same feed = a feed the active re-fetch cannot heal
# → the circuit opens and the alert escalates WARN → CRITICAL + the advisory
# position-reduction recommendation attaches. Cooldown lets it probe HALF_OPEN
# after 30 minutes of quiet so a recovered feed de-escalates.
_BREAKER_SERVICE_KEY = "feed-refetch"
_refetch_failure_breaker = CircuitBreaker(
    window_seconds=1800.0,
    threshold=3,
    cooldown_seconds=1800.0,
)


def get_refetch_failure_breaker() -> CircuitBreaker:
    """Return the module-level repeat-failure breaker (testing + health-checks)."""
    return _refetch_failure_breaker


def _criticality_to_severity(criticality: str, *, circuit_open: bool) -> AlertSeverity:
    """Map a feed's criticality + breaker state to an AlertSeverity tier.

    A SUSTAINED-failure (breaker OPEN) critical feed pages CRITICAL; otherwise a
    critical feed's repeated failure is WARN until the breaker opens. An
    ``important`` feed escalates at most to HIGH (it never blocks order flow).
    """
    if criticality == "critical":
        return AlertSeverity.CRITICAL if circuit_open else AlertSeverity.WARN
    if criticality == "important":
        return AlertSeverity.HIGH if circuit_open else AlertSeverity.WARN
    return AlertSeverity.INFO


def _severity_channels(severity: AlertSeverity) -> tuple[set[str], PagerDutySeverity | None]:
    """(channel_set, pagerduty_severity) for an AlertSeverity — mirrors
    consolidator_rules._severity_channels. CRITICAL → page; HIGH/WARN → telegram."""
    if severity is AlertSeverity.CRITICAL:
        return set(_PAGING_CHANNELS), _CRITICAL
    if severity is AlertSeverity.HIGH:
        return set(_PAGING_CHANNELS), _WARNING
    return {"telegram"}, None


def evaluate_stale_critical_feed(feed_id: str, criticality: str) -> dict[str, object]:
    """Decision tree (a)+(b) for a stale feed — emit the SILENT_RETRY refetch.

    Returns the recovery decision record. The order gate is ALREADY closed by
    the freshness_gate (this never re-implements that); we only name the bound
    Layer-0 refetch action that the recovery controller should fire.
    """
    refetch_action = _resolve_bound_action(feed_id)
    decision: dict[str, object] = {
        "feed_id": feed_id,
        "criticality": criticality,
        # (a) freshness_gate already blocks orders — recorded, not re-implemented.
        "orders_blocked_by_freshness_gate": criticality == "critical",
        # (b) the SILENT_RETRY — the feed's bound Layer-0 recovery action
        # (REST re-pull, or ws-session rotation for ws-sourced venues).
        "refetch_action": refetch_action,
        "recovery_step": "SILENT_RETRY",
    }
    log_event(
        "FEED_REFETCH_ALERT_ROUTED",
        details={
            "event_name": FEED_REFETCH_TRIGGERED,
            "feed_id": feed_id,
            "criticality": criticality,
            "recovery_step": "SILENT_RETRY",
        },
    )
    logger.info(
        "Stale %s feed=%s → firing %s (SILENT_RETRY); freshness_gate keeps orders blocked",
        criticality,
        feed_id,
        refetch_action,
    )
    return decision


def handle_refetch_failed(feed_id: str, criticality: str) -> dict[str, object]:
    """Decision tree (c)+(d) — a refetch attempt FAILED; escalate + advise.

    Records the failure into the per-feed breaker. WARN while the breaker is
    CLOSED; escalates to CRITICAL/HIGH (per criticality) once it OPENs (sustained
    failure the active re-fetch cannot heal). On breaker OPEN, attaches:
      - the audit-ack SLA (``lookup_sla`` for the escalated severity), and
      - an advisory position-reduction recommendation (the existing risk-advisory
        shape — recommendation only, no new actuator).
    Returns the routed alert record.
    """
    breaker = get_refetch_failure_breaker()
    transition = breaker.record_error(_BREAKER_SERVICE_KEY, feed_id)
    circuit_open = transition == STATE_OPEN or breaker.get_state(_BREAKER_SERVICE_KEY, feed_id) == STATE_OPEN
    failure_count = breaker.get_error_count(_BREAKER_SERVICE_KEY, feed_id)

    severity = _criticality_to_severity(criticality, circuit_open=circuit_open)
    channels, pd_severity = _severity_channels(severity)
    sla = lookup_sla(severity)

    alert: dict[str, object] = {
        "event_name": FEED_REFETCH_FAILED,
        "feed_id": feed_id,
        "criticality": criticality,
        "severity": severity.value,
        "failures_in_window": failure_count,
        "recovery_step": "ESCALATE",
        # audit-ack SLA cadence mapped to severity (lookup_sla is the SSOT).
        "audit_ack_due_seconds": sla.default_seconds,
        "message": (
            f"{_resolve_bound_action(feed_id)} FAILED (failures_in_window={failure_count}, severity={severity.value})"
        ),
    }

    if circuit_open:
        # (d) SUSTAINED failure → advisory position-reduction recommendation
        # (recommendation only — the runtime/operator owns the actual reduction).
        alert["recovery_step"] = "ADVISE_POSITION_REDUCTION"
        alert["advisory"] = "reduce_position"
        alert["escalation_reason"] = (
            f"{failure_count} refetch failures within {breaker.window:.0f}s for "
            f"feed={feed_id} — active re-fetch cannot heal; advise position reduction"
        )

    log_event(
        "FEED_REFETCH_ALERT_ROUTED",
        details={
            "event_name": FEED_REFETCH_FAILED,
            "feed_id": feed_id,
            "criticality": criticality,
            "severity": severity.value,
            "failures_in_window": failure_count,
            "recovery_step": alert["recovery_step"],
        },
    )
    logger.warning(
        "refetch FAILED feed=%s (failures_in_window=%d, severity=%s, step=%s)",
        feed_id,
        failure_count,
        severity.value,
        alert["recovery_step"],
    )
    route_event_with_explicit_channels(
        FEED_REFETCH_FAILED,
        alert,
        channels=channels,
        pd_severity=pd_severity,
    )
    return alert
