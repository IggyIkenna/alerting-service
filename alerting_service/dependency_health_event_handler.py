"""Dependency-health alert event handler.

Wires the previously-orphaned ``evaluate_dependency_health`` +
``evaluate_dependency_recovered`` rules (``rules/connectivity_rules.py``) into a
routeable handler, following the ``recon_freeze_event_handler`` pattern: a
probe-driven producer (``dependency_health_prober.py``) computes
``current_outage_seconds`` per dependency and calls
:func:`handle_dependency_health_payload` / :func:`handle_dependency_recovered_payload`,
which evaluate the rule ladder and route the resulting alert.

Routing (mirrors ``recon_freeze_event_handler._route_critical_alert``):
CRITICAL (``delivery_channel == "pagerduty+telegram"``) routes to PagerDuty +
Telegram via ``route_event_with_explicit_channels``; WARN / SEV1 / INFO
(``delivery_channel == "telegram"``) route to Telegram only. The rule already
computed severity + channel, so config routing rules are bypassed for this
family — same as the recon-freeze / recon-drift handlers.

Policy lookup: the handler holds a module-level registry of
``DependencyHealthPolicy`` keyed by ``dependency_id``, seeded by
:func:`set_dependency_policies` (the prober seeds it at startup from
``dependency_health_policies.yaml``). A payload whose ``dependency_id`` is not
registered is SKIPPED with a warning — never raised — so a config mismatch
cannot take down the caller's loop.

Issue: ``/plans/active/issues/dependency_health_alerting_never_wired_2026_08_12.md``
"""

from __future__ import annotations

import logging

from unified_api_contracts.dependency import DependencyHealthPolicy
from unified_trading_library import classify_and_emit_error, get_kill_switch_bus

from .notifiers.pagerduty import PagerDutySeverity
from .notifiers.router import route_event_with_explicit_channels
from .rules.connectivity_rules import (
    evaluate_dependency_health,
    evaluate_dependency_recovered,
)

logger = logging.getLogger(__name__)

_policies: dict[str, DependencyHealthPolicy] = {}

_PAGING_CHANNELS: frozenset[str] = frozenset({"pagerduty", "telegram"})
_TELEGRAM_ONLY: frozenset[str] = frozenset({"telegram"})
_CRITICAL: PagerDutySeverity = "critical"


def set_dependency_policies(policies: list[DependencyHealthPolicy]) -> None:
    """Seed the policy registry from ``dependency_health_policies.yaml``.

    Idempotent — a later call replaces the registry wholesale. Call this once at
    startup (or on config reload) before the prober begins probing.
    """
    global _policies
    _policies = {p.dependency_id: p for p in policies}
    logger.info("Dependency policy registry seeded with %d policies", len(_policies))


def get_dependency_policy(dependency_id: str) -> DependencyHealthPolicy | None:
    """Return the registered policy for ``dependency_id``, or None if unknown."""
    return _policies.get(dependency_id)


_KILL_SWITCH_RULE_ID = "DEPENDENCY_DEGRADED_CRITICAL"


def _maybe_arm_kill_switch(alert: dict[str, object], policy: DependencyHealthPolicy) -> None:
    """Arm the kill-switch bus for a CRITICAL dependency alert whose policy opts in.

    Sequencing per ``live_path_has_no_stale_producer_revocation_2026_08_14.md``
    item 1(c): only fires when BOTH (a) the alert is the SEV0/CRITICAL tier
    (``rule_id == "DEPENDENCY_DEGRADED_CRITICAL"``, per
    ``connectivity_rules.evaluate_dependency_health``) AND (b) the dependency's
    own policy carries a ``kill_switch_scope`` (opt-in — ``None`` for every
    dependency registered before 2026-08-22, and for the ~25+ external
    venue/RPC/cloud-infra dependencies that correctly stay alert-only). Fires
    the bus DIRECTLY (bypassing ``route_event`` / ``LIVE_ALERT_RULES``, the
    same way this handler already bypasses them for paging) so the existing
    config-driven routing for every other dependency's alert is completely
    unaffected by this addition.

    ``scope_key=None``: every scope this field is set to today (GLOBAL,
    STRATEGY) is correctly a wildcard here — a dependency-health probe is
    whole-service, not per-instance (see the ``strategy_service_health``
    policy comment in ``dependency_health_policies.yaml``), so there is no
    narrower key to extract from the payload the way venue/strategy-scoped
    KILL_SWITCH_* alerts have one.
    """
    if alert.get("rule_id") != _KILL_SWITCH_RULE_ID:
        return
    scope = policy.kill_switch_scope
    if scope is None:
        return
    reason = str(
        alert.get("message") or f"dependency {policy.dependency_id} SEV0 (rule_id={_KILL_SWITCH_RULE_ID})"
    )
    try:
        bus = get_kill_switch_bus()
        bus.fire(scope, None, reason=reason, fired_by="alerting-service:dependency_health")
        logger.critical(
            "Dependency-health kill switch armed: dependency_id=%s scope=%s reason=%s",
            policy.dependency_id,
            scope.value,
            reason,
        )
    except Exception as exc:
        # Paging must never be starved by a bus failure — this call always
        # runs AFTER channel dispatch (see _route_dependency_alert below).
        classify_and_emit_error(
            exc,
            service_name="alerting-service",
            operation="dependency_health_kill_switch",
        )


def _route_dependency_alert(alert: dict[str, object], policy: DependencyHealthPolicy) -> None:
    """Route one evaluated dependency alert to its computed channels, then
    (CRITICAL + the policy opts in) arm the kill switch."""
    delivery_channel = str(alert.get("delivery_channel", ""))  # noqa: qg-empty-fallback
    rule_id = str(alert.get("rule_id", "DEPENDENCY_DEGRADED"))  # noqa: qg-empty-fallback
    if "pagerduty" in delivery_channel:
        route_event_with_explicit_channels(
            rule_id,
            dict(alert),
            channels=set(_PAGING_CHANNELS),
            pd_severity=_CRITICAL,
        )
    elif delivery_channel == "telegram":
        route_event_with_explicit_channels(
            rule_id,
            dict(alert),
            channels=set(_TELEGRAM_ONLY),
            pd_severity=None,
        )
    else:
        logger.warning(
            "dependency alert dropped: unknown delivery_channel=%s rule_id=%s",
            delivery_channel,
            rule_id,
        )
        return
    _maybe_arm_kill_switch(alert, policy)


def handle_dependency_health_payload(payload: dict[str, object]) -> None:
    """Evaluate a dependency-outage payload and route any resulting alert.

    Args:
        payload: Must carry ``dependency_id`` + ``current_outage_seconds``.
            Optional ``venue`` / ``strategy_id`` forwarded to the rule. The
            matching ``DependencyHealthPolicy`` is looked up from the registry
            by ``dependency_id``.
    """
    dependency_id = str(payload.get("dependency_id", ""))  # noqa: qg-empty-fallback
    if not dependency_id:
        logger.warning("dependency health payload missing dependency_id — skipped")
        return
    policy = get_dependency_policy(dependency_id)
    if policy is None:
        logger.warning(
            "dependency health payload for unregistered dependency_id=%s — skipped",
            dependency_id,
        )
        return

    for alert in evaluate_dependency_health(payload, policy):
        _route_dependency_alert(alert, policy)


def handle_dependency_recovered_payload(payload: dict[str, object]) -> None:
    """Evaluate a dependency-recovery payload and route the INFO alert.

    Args:
        payload: Must carry ``dependency_id`` + ``previous_outage_seconds``
            (the outage duration that just ended).
    """
    dependency_id = str(payload.get("dependency_id", ""))  # noqa: qg-empty-fallback
    if not dependency_id:
        logger.warning("dependency recovered payload missing dependency_id — skipped")
        return
    policy = get_dependency_policy(dependency_id)
    if policy is None:
        logger.warning(
            "dependency recovered payload for unregistered dependency_id=%s — skipped",
            dependency_id,
        )
        return

    for alert in evaluate_dependency_recovered(payload, policy):
        _route_dependency_alert(alert, policy)
