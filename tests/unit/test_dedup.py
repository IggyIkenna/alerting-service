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

    def test_correlation_id_does_not_break_dedup(self) -> None:
        """A fresh per-delivery ``correlation_id`` (subscriber-injected) must NOT
        change the dedup key — else the recurring-event cooldowns never engage and
        the same DP_* alert re-pages every sweep (the 2026-08-10 refire storm)."""
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        base = {
            "vm_name": "mdps-cefi-2022-20260810-023628",
            "asset_group": "cefi",
            "umbrella": "BATCH",
            "cloud": "GCP",
            "run_log_tail": "POLARS AGGREGATED: 1440 1m candles",
        }
        a = {
            **base,
            "correlation_id": "74fa08c7-233f-49ef-84ce-8a3f695e13e6",
            "service_name": "dp-fleet-monitor",
            "source": "lifecycle-events-sub",
        }
        b = {
            **base,
            "correlation_id": "00e73fc9-2a0d-49d8-b6a6-eb01e28e397f",
            "service_name": "dp-fleet-monitor",
            "source": "lifecycle-events-sub",
        }
        assert dedup.is_duplicate("DP_VM_PREEMPTED", a) is False
        # different correlation_id (new delivery) but identical logical identity → suppressed
        assert dedup.is_duplicate("DP_VM_PREEMPTED", b) is True
        # a genuinely different VM is still a distinct alert
        c = {**base, "vm_name": "mdps-cefi-2022-20260810-023141", "correlation_id": "zzz"}
        assert dedup.is_duplicate("DP_VM_PREEMPTED", c) is False

    def test_attempted_age_hours_does_not_break_dedup(self) -> None:
        """DP-LIVE-004 (live_stream_watcher.check_live_capture_productivity) recomputes
        ``attempted_age_hours`` every ~15min sweep and it wasn't in the exact-match
        exclusion set — the climbing value defeated the 1800s DP_CRON_DID_NOT_FIRE
        cooldown every single sweep (2026-08-17 finding: 69 messages/24h, 2 live VMs)."""
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        base = {"vm_name": "mtds-live-cefi-consolidated-x", "venue": "BYBIT-FUTURES", "data_type": "trades"}
        a = {**base, "attempted_age_hours": 0.1, "last_captured_at": None}
        b = {**base, "attempted_age_hours": 0.2, "last_captured_at": None}
        assert dedup.is_duplicate("DP_CRON_DID_NOT_FIRE", a) is False
        assert dedup.is_duplicate("DP_CRON_DID_NOT_FIRE", b) is True
        # a different venue/data_type is still a distinct alert
        c = {**base, "data_type": "book_snapshot_5", "attempted_age_hours": 0.1}
        assert dedup.is_duplicate("DP_CRON_DID_NOT_FIRE", c) is False

    def test_generic_age_suffix_fields_are_volatile(self) -> None:
        """Belt-and-braces: ANY ``*_age_hours``/``*_age_days``/etc. key is excluded, not
        just the ones with an exact-match entry, so a future detector's own age-field
        name can't reproduce this bug class."""
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        base = {"vm_name": "some-vm"}
        assert dedup.is_duplicate("SOME_EVENT", {**base, "custom_metric_age_days": 1.0}) is False
        assert dedup.is_duplicate("SOME_EVENT", {**base, "custom_metric_age_days": 2.5}) is True


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
