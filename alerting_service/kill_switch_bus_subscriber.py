"""Subscribe alerting-service to the UTL KillSwitchBus.

Active halt policy: alerting ESCALATES on kill-switch fires. When a scope is
halted:
  - Priority of alerts matching the scope is boosted (CRITICAL).
  - Routing is switched to the on-call channel regardless of default routing.
  - A status banner is added to any alert mentioning the scope.

This is the opposite of the passive pattern used by PBM / PnL — alerting
should surface kill-switch activity prominently, not suppress it.

Consumed by: ServiceBootstrap via ``kill_switch_subscriber=on_bus_event``.
"""

from __future__ import annotations

import logging
import threading

# `unified_api_contracts.internal.KillSwitchScope` resolves to the risk-domain
# BaseModel (shape: entity_type / entity_id). We need the deployment-service
# StrEnum (shape: GLOBAL/CLIENT/VENUE/STRATEGY/ARCHETYPE/INSTRUMENT) — the two
# classes share a name but live in separate modules, so the deep import is
# the only unambiguous path until the facade grows a disambiguating alias.
from unified_api_contracts.internal.domain.deployment_service import (  # noqa: qg-deep-import
    KillSwitchScope,
)
from unified_trading_library import KillSwitchEvent, KillSwitchEventType

logger = logging.getLogger(__name__)


class _EscalationRegistry:
    """Process-local record of active scopes that warrant alert boosting."""

    def __init__(self) -> None:
        self._active: dict[KillSwitchScope, set[str | None]] = {}
        self._lock = threading.RLock()

    def fire(self, scope: KillSwitchScope, scope_key: str | None) -> None:
        with self._lock:
            self._active.setdefault(scope, set()).add(scope_key)

    def clear(self, scope: KillSwitchScope, scope_key: str | None) -> None:
        with self._lock:
            keys = self._active.get(scope)
            if keys is None:
                return
            keys.discard(scope_key)
            if not keys:
                del self._active[scope]

    def should_escalate(
        self,
        *,
        client_id: str | None = None,
        venue: str | None = None,
        strategy_id: str | None = None,
        instrument_id: str | None = None,
    ) -> bool:
        with self._lock:
            if self._active.get(KillSwitchScope.GLOBAL):
                return True
            if self._matches(KillSwitchScope.CLIENT, client_id):
                return True
            if self._matches(KillSwitchScope.VENUE, venue):
                return True
            if self._matches(KillSwitchScope.STRATEGY, strategy_id):
                return True
            return self._matches(KillSwitchScope.INSTRUMENT, instrument_id)

    def _matches(self, scope: KillSwitchScope, key: str | None) -> bool:
        active = self._active.get(scope)
        if not active:
            return False
        if None in active:
            return True
        return key is not None and key in active


_registry = _EscalationRegistry()


def should_escalate(
    *,
    client_id: str | None = None,
    venue: str | None = None,
    strategy_id: str | None = None,
    instrument_id: str | None = None,
) -> bool:
    """True if an alert touching the given entity should be escalated to CRITICAL."""
    return _registry.should_escalate(
        client_id=client_id,
        venue=venue,
        strategy_id=strategy_id,
        instrument_id=instrument_id,
    )


def on_bus_event(event: KillSwitchEvent) -> None:
    """ServiceBootstrap callback — records fire/clear events in the registry."""
    if event.event_type == KillSwitchEventType.FIRED:
        _registry.fire(event.scope, event.scope_key)
        logger.critical(
            "ALERTING kill-switch fired: scope=%s scope_key=%s reason=%s "
            "fired_by=%s — matching alerts will be boosted to CRITICAL",
            event.scope,
            event.scope_key,
            event.reason,
            event.fired_by,
        )
    elif event.event_type == KillSwitchEventType.CLEARED:
        _registry.clear(event.scope, event.scope_key)
        logger.info(
            "ALERTING kill-switch cleared: scope=%s scope_key=%s",
            event.scope,
            event.scope_key,
        )
