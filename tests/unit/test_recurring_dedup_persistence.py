"""Unit tests for RecurringCooldownState (GCS-persisted recurring-cooldown layer).

Regression coverage for dp_cron_did_not_fire_dedup_state_lost_on_redeploy_2026_08_18.md:
AlertDeduplicator._seen is in-process-only and is wiped by every fresh Cloud Run
revision; RecurringCooldownState is the durable layer that must survive that wipe for
_RECURRING_ALERT_COOLDOWNS-eligible events.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from alerting_service.core.recurring_dedup_persistence import RecurringCooldownState


def _fake_store(initial_state: dict[str, object] | None = None) -> MagicMock:
    store = MagicMock()
    store.read_cooldown_state.return_value = dict(initial_state or {})
    return store


class TestRecurringCooldownState:
    def test_first_occurrence_is_not_suppressed(self) -> None:
        state = RecurringCooldownState(store=_fake_store())
        assert state.should_suppress("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"}, ttl_seconds=1800.0) is False

    def test_recorded_occurrence_is_suppressed_within_window(self) -> None:
        state = RecurringCooldownState(store=_fake_store())
        details = {"vm_name": "vm-a"}
        with patch("alerting_service.core.recurring_dedup_persistence.time.time", return_value=1000.0):
            state.record("DP_CRON_DID_NOT_FIRE", details)
        with patch("alerting_service.core.recurring_dedup_persistence.time.time", return_value=1500.0):
            assert state.should_suppress("DP_CRON_DID_NOT_FIRE", details, ttl_seconds=1800.0) is True

    def test_recorded_occurrence_expires_past_window(self) -> None:
        state = RecurringCooldownState(store=_fake_store())
        details = {"vm_name": "vm-a"}
        with patch("alerting_service.core.recurring_dedup_persistence.time.time", return_value=1000.0):
            state.record("DP_CRON_DID_NOT_FIRE", details)
        with patch("alerting_service.core.recurring_dedup_persistence.time.time", return_value=2801.0):
            assert state.should_suppress("DP_CRON_DID_NOT_FIRE", details, ttl_seconds=1800.0) is False

    def test_survives_a_fresh_instance_seeded_from_gcs(self) -> None:
        """The core regression: a NEW RecurringCooldownState (a fresh container after a
        redeploy) still suppresses because it loads prior state from the store — unlike
        AlertDeduplicator._seen, which starts empty every time."""
        details = {"vm_name": "vm-a", "venue": "BYBIT-FUTURES"}
        backing_json: dict[str, object] = {}
        store = _fake_store()
        store.read_cooldown_state.side_effect = lambda: dict(backing_json)

        def _capture_write(state: dict[str, object]) -> None:
            backing_json.clear()
            backing_json.update(state)

        store.write_cooldown_state.side_effect = _capture_write

        first = RecurringCooldownState(store=store)
        with patch("alerting_service.core.recurring_dedup_persistence.time.time", return_value=1000.0):
            first.record("DP_CRON_DID_NOT_FIRE", details)

        # Simulate a Cloud Run revision redeploy: a brand-new process, brand-new instance.
        second = RecurringCooldownState(store=store)
        with patch("alerting_service.core.recurring_dedup_persistence.time.time", return_value=1050.0):
            assert second.should_suppress("DP_CRON_DID_NOT_FIRE", details, ttl_seconds=1800.0) is True

    def test_different_identity_is_not_suppressed(self) -> None:
        state = RecurringCooldownState(store=_fake_store())
        with patch("alerting_service.core.recurring_dedup_persistence.time.time", return_value=1000.0):
            state.record("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"})
        with patch("alerting_service.core.recurring_dedup_persistence.time.time", return_value=1001.0):
            assert state.should_suppress("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-b"}, ttl_seconds=1800.0) is False

    def test_load_failure_fails_open_never_suppresses(self) -> None:
        store = _fake_store()
        store.read_cooldown_state.side_effect = RuntimeError("GCS unavailable")
        state = RecurringCooldownState(store=store)
        assert state.should_suppress("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"}, ttl_seconds=1800.0) is False

    def test_persist_failure_does_not_raise(self) -> None:
        store = _fake_store()
        store.write_cooldown_state.side_effect = RuntimeError("GCS unavailable")
        state = RecurringCooldownState(store=store)
        state.record("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"})  # must not raise

    def test_load_happens_at_most_once(self) -> None:
        store = _fake_store()
        state = RecurringCooldownState(store=store)
        state.should_suppress("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"}, ttl_seconds=1800.0)
        state.should_suppress("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-b"}, ttl_seconds=1800.0)
        state.record("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-c"})
        assert store.read_cooldown_state.call_count == 1
