"""Unit tests for the alert deduplicator."""

from __future__ import annotations

from unittest.mock import patch

from alerting_service.core.dedup import AlertDeduplicator


class TestAlertDeduplicator:
    def test_first_event_is_not_duplicate(self) -> None:
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        assert dedup.is_duplicate("EVENT_A", {"key": "val"}) is False

    def test_same_event_within_ttl_is_duplicate(self) -> None:
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        dedup.is_duplicate("EVENT_A", {"key": "val"})
        assert dedup.is_duplicate("EVENT_A", {"key": "val"}) is True

    def test_different_event_name_is_not_duplicate(self) -> None:
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        dedup.is_duplicate("EVENT_A", {"key": "val"})
        assert dedup.is_duplicate("EVENT_B", {"key": "val"}) is False

    def test_same_name_different_details_is_not_duplicate(self) -> None:
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        dedup.is_duplicate("EVENT_A", {"key": "val1"})
        assert dedup.is_duplicate("EVENT_A", {"key": "val2"}) is False

    def test_event_after_ttl_is_not_duplicate(self) -> None:
        dedup = AlertDeduplicator(ttl_seconds=10.0)
        # Record the event at time 0
        with patch("alerting_service.core.dedup.time.monotonic", return_value=0.0):
            dedup.is_duplicate("EVENT_A", {"key": "val"})

        # Check at time 11 (past TTL)
        with patch("alerting_service.core.dedup.time.monotonic", return_value=11.0):
            assert dedup.is_duplicate("EVENT_A", {"key": "val"}) is False

    def test_event_within_ttl_is_duplicate(self) -> None:
        dedup = AlertDeduplicator(ttl_seconds=10.0)
        with patch("alerting_service.core.dedup.time.monotonic", return_value=0.0):
            dedup.is_duplicate("EVENT_A", {"key": "val"})

        with patch("alerting_service.core.dedup.time.monotonic", return_value=5.0):
            assert dedup.is_duplicate("EVENT_A", {"key": "val"}) is True

    def test_empty_details(self) -> None:
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        assert dedup.is_duplicate("EVENT_A", {}) is False
        assert dedup.is_duplicate("EVENT_A", {}) is True

    def test_expired_entries_are_evicted(self) -> None:
        dedup = AlertDeduplicator(ttl_seconds=5.0)
        with patch("alerting_service.core.dedup.time.monotonic", return_value=0.0):
            dedup.is_duplicate("EVENT_A", {"k": "v"})

        assert len(dedup._seen) == 1

        # After TTL, eviction should clear the old entry
        with patch("alerting_service.core.dedup.time.monotonic", return_value=6.0):
            dedup.is_duplicate("EVENT_B", {"k": "v"})

        # EVENT_A should have been evicted
        assert len(dedup._seen) == 1

    def test_default_ttl_is_60(self) -> None:
        dedup = AlertDeduplicator()
        assert dedup._ttl == 60.0

    def test_key_determinism(self) -> None:
        """Same inputs always produce the same key."""
        key1 = AlertDeduplicator._make_key("E", {"a": 1, "b": 2})
        key2 = AlertDeduplicator._make_key("E", {"b": 2, "a": 1})
        assert key1 == key2
