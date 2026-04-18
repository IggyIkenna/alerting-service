"""Tests for alerting kill_switch_bus_subscriber — active escalation policy."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from unified_api_contracts.internal.domain.deployment_service import KillSwitchScope
from unified_trading_library import KillSwitchEvent, KillSwitchEventType

from alerting_service.kill_switch_bus_subscriber import (
    _registry,
    on_bus_event,
    should_escalate,
)


def _event(
    event_type: KillSwitchEventType,
    scope: KillSwitchScope,
    scope_key: str | None,
) -> KillSwitchEvent:
    return KillSwitchEvent(
        event_type=event_type,
        scope=scope,
        scope_key=scope_key,
        reason="test",
        fired_by="test",
        fired_at=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def _reset() -> None:
    _registry._active.clear()
    yield
    _registry._active.clear()


class TestEscalation:
    def test_no_fires_no_escalation(self) -> None:
        assert should_escalate() is False
        assert should_escalate(venue="any") is False

    def test_venue_fires_escalate_venue_alerts(self) -> None:
        on_bus_event(_event(KillSwitchEventType.FIRED, KillSwitchScope.VENUE, "binance"))
        assert should_escalate(venue="binance") is True
        assert should_escalate(venue="okx") is False

    def test_client_fires_escalate_client_alerts(self) -> None:
        on_bus_event(_event(KillSwitchEventType.FIRED, KillSwitchScope.CLIENT, "alpha"))
        assert should_escalate(client_id="alpha") is True
        assert should_escalate(client_id="beta") is False

    def test_global_escalates_everything(self) -> None:
        on_bus_event(_event(KillSwitchEventType.FIRED, KillSwitchScope.GLOBAL, None))
        assert should_escalate(venue="any") is True
        assert should_escalate(client_id="any") is True
        assert should_escalate(strategy_id="any") is True
        assert should_escalate(instrument_id="any") is True

    def test_wildcard_escalates_all_keys_under_scope(self) -> None:
        on_bus_event(_event(KillSwitchEventType.FIRED, KillSwitchScope.VENUE, None))
        assert should_escalate(venue="binance") is True
        assert should_escalate(venue="okx") is True
        assert should_escalate(client_id="unrelated") is False


class TestClear:
    def test_clear_stops_escalation(self) -> None:
        on_bus_event(_event(KillSwitchEventType.FIRED, KillSwitchScope.VENUE, "binance"))
        on_bus_event(_event(KillSwitchEventType.CLEARED, KillSwitchScope.VENUE, "binance"))
        assert should_escalate(venue="binance") is False

    def test_clear_of_unknown_is_noop(self) -> None:
        on_bus_event(_event(KillSwitchEventType.CLEARED, KillSwitchScope.VENUE, "ghost"))
        assert should_escalate(venue="ghost") is False

    def test_multiple_fires_partial_clear(self) -> None:
        on_bus_event(_event(KillSwitchEventType.FIRED, KillSwitchScope.VENUE, "binance"))
        on_bus_event(_event(KillSwitchEventType.FIRED, KillSwitchScope.VENUE, "okx"))
        on_bus_event(_event(KillSwitchEventType.CLEARED, KillSwitchScope.VENUE, "binance"))
        assert should_escalate(venue="binance") is False
        assert should_escalate(venue="okx") is True


class TestInstrumentAndStrategy:
    def test_instrument_scope(self) -> None:
        on_bus_event(_event(KillSwitchEventType.FIRED, KillSwitchScope.INSTRUMENT, "DOGE"))
        assert should_escalate(instrument_id="DOGE") is True

    def test_strategy_scope(self) -> None:
        on_bus_event(_event(KillSwitchEventType.FIRED, KillSwitchScope.STRATEGY, "mm"))
        assert should_escalate(strategy_id="mm") is True
