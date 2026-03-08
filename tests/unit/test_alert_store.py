"""Unit tests for alerting_service.core.alert_store."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from unified_internal_contracts import AlertEvent

from alerting_service.core.alert_store import AlertStore


def _make_event(rule_id: str = "rule-1", alert_id: str = "alert-1") -> AlertEvent:
    return AlertEvent(
        alert_id=alert_id,
        rule_id=rule_id,
        triggered_at=datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC),
        severity="WARNING",
        message="Test alert",
        metric_value=0.95,
        threshold=0.90,
    )


class TestAlertStoreCooldown:
    def test_is_cooled_down_true_when_never_fired(self) -> None:
        store = AlertStore()
        assert store.is_cooled_down("unknown-rule", 60) is True

    def test_is_cooled_down_true_after_zero_cooldown(self) -> None:
        store = AlertStore()
        event = _make_event()
        store.record_fired(event)
        # triggered_at is 2026-03-08 12:00:00 UTC (in the past).
        # With 0 seconds cooldown, elapsed time (many seconds) > 0, so it IS cooled down.
        assert store.is_cooled_down(event.rule_id, 0) is True

    def test_is_cooled_down_false_within_cooldown_window(self) -> None:
        store = AlertStore()
        event = _make_event()
        store.record_fired(event)
        # triggered_at is 2026-03-08 12:00:00 UTC (past); 999999s cooldown ensures not cooled down
        # Since triggered_at is in the past and current time > triggered_at, delta is positive.
        # For a very large cooldown, the elapsed time < cooldown → not cooled down.
        assert store.is_cooled_down(event.rule_id, 999_999_999) is False


class TestAlertStoreRecordFired:
    def test_record_fired_adds_to_recent_events(self) -> None:
        store = AlertStore()
        event = _make_event()
        store.record_fired(event)
        assert event in store.get_recent_events()

    def test_get_recent_events_respects_limit(self) -> None:
        store = AlertStore()
        for i in range(10):
            store.record_fired(_make_event(alert_id=f"alert-{i}"))
        recent = store.get_recent_events(limit=3)
        assert len(recent) == 3

    def test_get_recent_events_default_limit(self) -> None:
        store = AlertStore()
        for i in range(5):
            store.record_fired(_make_event(alert_id=f"alert-{i}"))
        assert len(store.get_recent_events()) == 5

    def test_recent_events_trimmed_when_exceeds_1000(self) -> None:
        store = AlertStore()
        for i in range(1001):
            store.record_fired(_make_event(alert_id=f"alert-{i}"))
        # Should be trimmed to 500
        assert len(store._recent_events) == 500


class TestAlertStoreSubscriptions:
    @pytest.mark.asyncio
    async def test_subscribe_returns_queue(self) -> None:
        store = AlertStore()
        q = store.subscribe()
        assert isinstance(q, asyncio.Queue)

    @pytest.mark.asyncio
    async def test_publish_delivers_to_subscriber(self) -> None:
        store = AlertStore()
        q = store.subscribe()
        event = _make_event()
        await store.publish(event)
        received = q.get_nowait()
        assert received == event

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_queue(self) -> None:
        store = AlertStore()
        q = store.subscribe()
        store.unsubscribe(q)
        event = _make_event()
        await store.publish(event)
        assert q.empty()

    @pytest.mark.asyncio
    async def test_publish_skips_full_queue(self) -> None:
        store = AlertStore()
        q = store.subscribe()
        # Fill queue to capacity (maxsize=200)
        for i in range(200):
            q.put_nowait(_make_event(alert_id=f"fill-{i}"))
        # Should not raise even though queue is full
        event = _make_event(alert_id="overflow")
        await store.publish(event)
        assert q.full()
