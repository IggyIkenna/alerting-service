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


class TestStableIdentityDedup:
    """2026-06-24 fix — volatile/render fields are excluded from the dedup key so a
    recurring same-vm lifecycle alert collapses to ONE key across sweeps + shapes."""

    def test_climbing_heartbeat_age_is_still_a_duplicate(self) -> None:
        """The SAME vm's DP_VM_STALL with a climbing age must dedup (the flood bug)."""
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        base = {"vm_name": "tradfi-bf-cme-ohlcv-1m-cl-2025", "asset_group": "tradfi"}
        assert dedup.is_duplicate("DP_VM_STALL", {**base, "heartbeat_age_min": 28, "message": "…28m stale"}) is False
        # next sweep: age climbed to 33m, message changed — MUST still be a duplicate
        assert dedup.is_duplicate("DP_VM_STALL", {**base, "heartbeat_age_min": 33, "message": "…33m stale"}) is True

    def test_rich_and_bare_emit_shapes_collapse_to_one(self) -> None:
        """A rich payload (umbrella/cloud) + a bare one for the SAME vm dedup together."""
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        vm = "tradfi-bf-cme-ohlcv-1m-cl-2025"
        rich = {"vm_name": vm, "asset_group": "tradfi", "umbrella": "BATCH", "cloud": "GCP", "heartbeat_age_min": 28}
        bare = {"vm_name": vm, "asset_group": "tradfi"}
        assert dedup.is_duplicate("DP_VM_STALL", rich) is False
        assert dedup.is_duplicate("DP_VM_STALL", bare) is True  # same identity → suppressed

    def test_different_vm_is_not_a_duplicate(self) -> None:
        """Distinct VMs (the identity field differs) still alert independently."""
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        assert dedup.is_duplicate("DP_VM_STALL", {"vm_name": "vm-a", "heartbeat_age_min": 28}) is False
        assert dedup.is_duplicate("DP_VM_STALL", {"vm_name": "vm-b", "heartbeat_age_min": 28}) is False

    def test_volatile_only_difference_dedups_but_identity_difference_does_not(self) -> None:
        """venue/instrument (identity) distinguish; timestamp/log_url (volatile) do not."""
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        a = {"venue": "BYBIT", "instrument": "BTCUSDT", "timestamp": "t1", "log_url": "u1"}
        b = {"venue": "BYBIT", "instrument": "BTCUSDT", "timestamp": "t2", "log_url": "u2"}
        c = {"venue": "BYBIT", "instrument": "ETHUSDT", "timestamp": "t3"}
        assert dedup.is_duplicate("TICK_STALENESS", a) is False
        assert dedup.is_duplicate("TICK_STALENESS", b) is True  # only volatile fields differ
        assert dedup.is_duplicate("TICK_STALENESS", c) is False  # different instrument → distinct


class TestTtlOverride:
    """A per-call ttl_override gives recurring WARN alerts a cooldown >= sweep interval."""

    def test_override_holds_entry_past_default_ttl(self) -> None:
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        det = {"vm_name": "vm-x"}
        with patch("alerting_service.core.dedup.time.monotonic", return_value=0.0):
            assert dedup.is_duplicate("DP_VM_STALL", det, ttl_override=1800.0) is False
        # at t=120s the default 60s window would have expired, but the 1800s override holds it
        with patch("alerting_service.core.dedup.time.monotonic", return_value=120.0):
            assert dedup.is_duplicate("DP_VM_STALL", det, ttl_override=1800.0) is True
        # past the override window it re-fires (the resolve-and-recur re-ping)
        with patch("alerting_service.core.dedup.time.monotonic", return_value=1801.0):
            assert dedup.is_duplicate("DP_VM_STALL", det, ttl_override=1800.0) is False

    def test_no_override_uses_default_ttl(self) -> None:
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        det = {"vm_name": "vm-y"}
        with patch("alerting_service.core.dedup.time.monotonic", return_value=0.0):
            dedup.is_duplicate("OTHER_EVENT", det)
        with patch("alerting_service.core.dedup.time.monotonic", return_value=61.0):
            assert dedup.is_duplicate("OTHER_EVENT", det) is False  # default 60s expired
