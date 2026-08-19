"""Unit tests for router._is_duplicate_alert's persisted-cooldown wiring.

Regression coverage for dp_cron_did_not_fire_dedup_state_lost_on_redeploy_2026_08_18.md
— proves the GCS-persisted RecurringCooldownState layer is consulted for
_RECURRING_ALERT_COOLDOWNS-eligible events, is bypassed for everything else (no added
GCS I/O on the general hot path), and never touched during batch replay.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from alerting_service.notifiers import router


def _reset_deduplicator() -> None:
    """Simulate a fresh Cloud Run revision: AlertDeduplicator._seen starts empty."""
    router._deduplicator._seen.clear()


class TestIsDuplicateAlertRecurringWiring:
    def setup_method(self) -> None:
        _reset_deduplicator()

    def teardown_method(self) -> None:
        _reset_deduplicator()

    def test_non_recurring_event_never_touches_persisted_layer(self) -> None:
        with (
            patch("alerting_service.notifiers.router._dedup_window_for", return_value=None),
            patch("alerting_service.notifiers.router._get_recurring_cooldown_state") as mock_get_state,
        ):
            assert router._is_duplicate_alert("SOME_ONE_OFF_EVENT", {"k": "v"}) is False
            mock_get_state.assert_not_called()

    def test_recurring_event_persisted_suppression_wins_over_empty_in_memory_state(self) -> None:
        """The core regression: AlertDeduplicator._seen is empty (fresh process) but the
        persisted layer says still-cooling-down — must still suppress."""
        mock_state = MagicMock()
        mock_state.should_suppress.return_value = True
        with (
            patch("alerting_service.notifiers.router._dedup_window_for", return_value=1800.0),
            patch("alerting_service.notifiers.router._get_recurring_cooldown_state", return_value=mock_state),
        ):
            assert router._is_duplicate_alert("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"}) is True
        mock_state.record.assert_not_called()

    def test_recurring_event_first_occurrence_delivers_and_records(self) -> None:
        mock_state = MagicMock()
        mock_state.should_suppress.return_value = False
        with (
            patch("alerting_service.notifiers.router._dedup_window_for", return_value=1800.0),
            patch("alerting_service.notifiers.router._get_recurring_cooldown_state", return_value=mock_state),
        ):
            assert router._is_duplicate_alert("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"}) is False
        mock_state.record.assert_called_once_with("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"})

    def test_batch_mode_never_touches_persisted_layer(self) -> None:
        router.set_batch_mode(True)
        try:
            with (
                patch("alerting_service.notifiers.router._dedup_window_for", return_value=1800.0),
                patch("alerting_service.notifiers.router._get_recurring_cooldown_state") as mock_get_state,
            ):
                router._is_duplicate_alert("DP_CRON_DID_NOT_FIRE", {"vm_name": "vm-a"})
                mock_get_state.assert_not_called()
        finally:
            router.set_batch_mode(False)

    def test_already_suppressed_in_memory_does_not_record_again(self) -> None:
        mock_state = MagicMock()
        mock_state.should_suppress.return_value = False
        with (
            patch("alerting_service.notifiers.router._dedup_window_for", return_value=1800.0),
            patch("alerting_service.notifiers.router._get_recurring_cooldown_state", return_value=mock_state),
        ):
            details = {"vm_name": "vm-a"}
            assert router._is_duplicate_alert("DP_CRON_DID_NOT_FIRE", details) is False
            mock_state.record.reset_mock()
            # Second call within the in-process TTL is caught by _deduplicator itself —
            # the persisted layer must not double-record.
            assert router._is_duplicate_alert("DP_CRON_DID_NOT_FIRE", details) is True
            mock_state.record.assert_not_called()
