"""Kill-switch rule matching + bus-publish hook for the event router.

Split out of ``router.py`` 2026-06-12 (codex_violations_ratchet_to_five plan,
Phase 1.5 — file-size <900). ``router.py`` re-binds every symbol under its
original name, so the test surface (``router._publish_kill_switch_event`` is a
patched target) and in-repo consumers resolve unchanged. Calls to patched
collaborators (``log_event``) route through the router module namespace at
call time so existing ``patch("...router.log_event")`` keeps intercepting.
"""

from __future__ import annotations

import logging
from fnmatch import fnmatch

from unified_api_contracts import LIVE_ALERT_RULES, AlertRule, KillSwitchScope
from unified_trading_library import classify_and_emit_error, get_kill_switch_bus

# Late-bound back-reference: router imports this module at its own import time,
# so this resolves to the (then partially-initialised) router module object in
# sys.modules; attributes are only accessed at call time. This is the
# established facade-namespace pattern (MTDS sentinels / instruments _pkg_ref).
from alerting_service.notifiers import router as _router

logger = logging.getLogger(__name__)


def find_kill_switch_rule(event_name: str) -> AlertRule | None:
    """Return the LIVE_ALERT_RULES entry that matches event_name AND triggers a kill-switch.

    KILL_SWITCH_* AlertCodes carry ``triggers_kill_switch=True`` + a non-None
    ``kill_switch_scope`` per UAC ``alerting_service_live_rules_2026_05_07``
    Phase 1 + the 2026-05-08 publisher-hook migration. Non-kill-switch codes
    return ``None`` (the hook is skipped — channel dispatch still happens).

    Defensive about UAC version-skew: if the running UAC predates the
    ``kill_switch_scope`` field (in-flight ship), we surface a clear log warning
    + return None rather than raising — the alert still pages on-call via the
    normal channel dispatch path.
    """
    for rule in LIVE_ALERT_RULES:
        if not fnmatch(event_name, rule.event_pattern):
            continue
        if not rule.triggers_kill_switch:
            return None
        scope = rule.kill_switch_scope
        if scope is None:
            logger.warning(
                "Kill-switch publisher hook: AlertRule for %s has triggers_kill_switch=True "
                "but kill_switch_scope is None — UAC field not yet shipped or seed not "
                "populated; skipping bus.fire() and routing channels only.",
                event_name,
            )
            return None
        return rule
    return None


def resolve_scope_key(scope: KillSwitchScope, details: dict[str, object]) -> str | None:
    """Pick a scope_key from the alert payload appropriate for this scope.

    Per ``KillSwitchBus`` semantics, scope_key=None means "wildcard halt all keys
    under this scope" — only intended for GLOBAL. For VENUE / STRATEGY / etc.,
    we extract the corresponding identifier from ``details`` (set by the alert
    emitter); if missing, log + still fire with None (operator-visible
    over-broad halt is safer than no halt).
    """
    if scope is KillSwitchScope.GLOBAL:
        return None
    key_field_by_scope: dict[KillSwitchScope, str] = {
        KillSwitchScope.VENUE: "venue",
        KillSwitchScope.CLIENT: "client_id",
        KillSwitchScope.STRATEGY: "strategy_id",
        KillSwitchScope.ARCHETYPE: "archetype",
        KillSwitchScope.INSTRUMENT: "instrument_id",
    }
    field_name = key_field_by_scope.get(scope)
    if field_name is None:
        return None
    raw_value = details.get(field_name)
    if raw_value is None:
        logger.warning(
            "Kill-switch publisher hook: scope=%s requires details.%s but field "
            "is missing from alert payload; firing with scope_key=None (over-broad halt).",
            scope,
            field_name,
        )
        return None
    return str(raw_value)


def publish_kill_switch_event(event_name: str, details: dict[str, object], alert_id: str) -> None:
    """Emit a typed KillSwitchEvent to the in-process bus on KILL_SWITCH_* alerts.

    Side-effect-free vs the channel dispatch: if the bus publish raises (e.g. a
    subscriber crashes), the exception is swallowed + logged — the channel
    dispatch must remain reliable. Per the operator directive 2026-05-08, the
    bus subscribers (execution-service / strategy-service / position-balance-monitor)
    own their own retry / circuit-breaker behaviour.

    Reference: ``alerting_service_live_rules_2026_05_07`` Phase 2 → Migrated
    issues 2026-05-08 → "Kill-switch publisher hook."
    """
    rule = find_kill_switch_rule(event_name)
    if rule is None:
        return
    scope = rule.kill_switch_scope
    if scope is None:
        # Already logged in _find_kill_switch_rule — defensive-double check.
        return
    scope_key = resolve_scope_key(scope, details)
    reason = str(details.get("message") or details.get("reason") or f"alert {event_name} fired (alert_id={alert_id})")
    try:
        bus = get_kill_switch_bus()
        bus.fire(
            scope,
            scope_key,
            reason=reason,
            fired_by="alerting-service",
        )
        _router.log_event(
            "KILL_SWITCH_PUBLISHED",
            details={
                "event_name": event_name,
                "alert_id": alert_id,
                "scope": scope.value,
                "scope_key": scope_key,
            },
        )
    except Exception as exc:
        # Channel dispatch must never fail because the kill-switch bus failed.
        # Log loud + classify so the operator can diagnose, but DO NOT raise.
        classify_and_emit_error(
            exc,
            service_name="alerting-service",
            operation="publish_kill_switch_event",
        )
        _router.log_event(
            "KILL_SWITCH_PUBLISH_FAILED",
            details={
                "event_name": event_name,
                "alert_id": alert_id,
                "scope": scope.value,
                "scope_key": scope_key,
                "error": str(exc),
            },
        )
