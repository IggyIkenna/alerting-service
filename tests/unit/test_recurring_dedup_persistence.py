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

    def test_cached_load_happens_at_most_once_for_should_suppress(self) -> None:
        """The `_ensure_loaded` cache primes on the first call and is never re-read by
        `should_suppress` — only `record()`'s own merge-read (see below) issues a second
        read, and only once per `record()` call, not once per `should_suppress`."""
        store = _fake_store()
        state = RecurringCooldownState(store=store)
        state.should_suppress("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"}, ttl_seconds=1800.0)
        state.should_suppress("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-b"}, ttl_seconds=1800.0)
        assert store.read_cooldown_state.call_count == 1
        state.record("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-c"})
        assert store.read_cooldown_state.call_count == 2

    def test_record_merges_against_latest_durable_state_not_a_blind_overwrite(self) -> None:
        """Regression for the live 2026-08-19 finding: two RecurringCooldownState instances
        (e.g. old+new Cloud Run revision overlapping during a redeploy) each load state once
        at construction time and diverge from there. Without a merge-before-write, whichever
        instance's `record()` runs LAST wins and silently drops every identity the OTHER
        instance recorded — defeating the cooldown for those identities exactly like the
        pre-fix in-process-only dict did. `record()` must merge against a fresh read of the
        durable store, not just persist its own local `_last_emitted_at` view."""
        backing_json: dict[str, object] = {}
        store = _fake_store()
        store.read_cooldown_state.side_effect = lambda: dict(backing_json)

        def _capture_write(state: dict[str, object]) -> None:
            backing_json.clear()
            backing_json.update(state)

        store.write_cooldown_state.side_effect = _capture_write

        # Both instances load the SAME (empty) initial state, simulating two processes
        # that started before either had recorded anything.
        instance_a = RecurringCooldownState(store=store)
        instance_b = RecurringCooldownState(store=store)
        instance_a.should_suppress("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"}, ttl_seconds=1800.0)
        instance_b.should_suppress("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-b"}, ttl_seconds=1800.0)

        with patch("alerting_service.core.recurring_dedup_persistence.time.time", return_value=1000.0):
            instance_a.record("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"})
        with patch("alerting_service.core.recurring_dedup_persistence.time.time", return_value=1010.0):
            instance_b.record("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-b"})

        # A THIRD instance (the next redeploy) must see BOTH identities as cooling down --
        # a blind overwrite would have let instance_b's write erase vm-a's record.
        instance_c = RecurringCooldownState(store=store)
        with patch("alerting_service.core.recurring_dedup_persistence.time.time", return_value=1020.0):
            assert instance_c.should_suppress("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"}, ttl_seconds=1800.0) is True
            assert instance_c.should_suppress("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-b"}, ttl_seconds=1800.0) is True
