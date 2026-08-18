"""Unit tests for the GCS-durable escalation ladder (escalation_ladder.py,
Phase 3 of alerting_service_escalation_ladder_centralization_2026_08_18.md).

Mirrors test_circuit_breaker.py's scenario coverage (CLOSED/OPEN/HALF_OPEN
transitions, independent per-key state) plus
test_orchestrator_dispatch_budget.py's fake-GCS-storage-client shape (this
module's state is durable, circuit_breaker.py's is not), since this module
is explicitly modeled on the former's state shape but built like the latter's
persistence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alerting_service import escalation_ladder as ladder
from alerting_service.circuit_breaker import STATE_CLOSED, STATE_OPEN

pytestmark = pytest.mark.unit


class _FakeStorageClient:
    def __init__(self, store: dict[tuple[str, str], bytes] | None = None) -> None:
        self.store: dict[tuple[str, str], bytes] = store if store is not None else {}

    def download_bytes(self, bucket: str, blob_path: str) -> bytes:
        key = (bucket, blob_path)
        if key not in self.store:
            raise FileNotFoundError(blob_path)
        return self.store[key]

    def upload_bytes(self, bucket: str, blob_path: str, data: bytes, content_type: str | None = None) -> str:
        self.store[(bucket, blob_path)] = data
        return f"gs://{bucket}/{blob_path}"


def _patch_ladder_gcs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bucket: str = "alerting-service-test-proj",
    store: dict[tuple[str, str], bytes] | None = None,
) -> _FakeStorageClient:
    client = _FakeStorageClient(store)
    monkeypatch.setattr(ladder, "state_bucket", lambda: bucket)
    monkeypatch.setattr(ladder, "get_storage_client", lambda: client)
    return client


# ── record_occurrence: threshold crossing ────────────────────────────────────


def test_occurrences_below_threshold_stay_muted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ladder_gcs(monkeypatch)
    assert ladder.record_occurrence("id-a", window_seconds=1800.0) == ""
    assert ladder.record_occurrence("id-a", window_seconds=1800.0) == ""


def test_nth_occurrence_crosses_threshold_and_fires_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ladder_gcs(monkeypatch)
    assert ladder.record_occurrence("id-a", window_seconds=1800.0, threshold=3) == ""
    assert ladder.record_occurrence("id-a", window_seconds=1800.0, threshold=3) == ""
    assert ladder.record_occurrence("id-a", window_seconds=1800.0, threshold=3) == STATE_OPEN

    state = ladder.get_state("id-a")
    assert state is not None
    assert state.state == STATE_OPEN
    assert state.occurrence_count == 3
    assert state.rung == 1
    assert state.last_escalated_at is not None


def test_open_state_does_not_refire_on_every_subsequent_occurrence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLOSED->OPEN transition fires exactly once per crossing --
    further occurrences while still OPEN (within the cooldown window) stay
    quiet every time, mirroring circuit_breaker's "no transition -> empty
    string" contract (not just the immediate next call)."""
    _patch_ladder_gcs(monkeypatch)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    for i in range(3):
        ladder.record_occurrence("id-a", window_seconds=1800.0, threshold=3, now=now + timedelta(seconds=i))

    for i in range(3, 8):
        transition = ladder.record_occurrence(
            "id-a", window_seconds=1800.0, threshold=3, now=now + timedelta(seconds=i)
        )
        assert transition == ""

    state = ladder.get_state("id-a")
    assert state is not None
    assert state.state == STATE_OPEN
    assert state.rung == 1
    assert state.occurrence_count == 8  # every occurrence still counted for audit visibility


def test_closed_window_resets_after_a_quiet_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLOSED identity that goes quiet for longer than window_seconds
    starts a fresh escalation cycle rather than accumulating
    occurrence_count forever."""
    _patch_ladder_gcs(monkeypatch)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    ladder.record_occurrence("id-a", window_seconds=60.0, threshold=3, now=now)
    ladder.record_occurrence("id-a", window_seconds=60.0, threshold=3, now=now + timedelta(seconds=10))

    # Quiet gap > window_seconds -- resets, does NOT cross threshold even
    # though this is technically the 3rd occurrence ever recorded.
    transition = ladder.record_occurrence("id-a", window_seconds=60.0, threshold=3, now=now + timedelta(seconds=200))
    assert transition == ""

    state = ladder.get_state("id-a")
    assert state is not None
    assert state.occurrence_count == 1
    assert state.state == STATE_CLOSED


def test_half_open_recurrence_re_escalates_and_bumps_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    """An occurrence AFTER the cooldown has elapsed since last_escalated_at
    ages OPEN->HALF_OPEN and the SAME occurrence immediately re-fires
    HALF_OPEN->OPEN (a genuine recurrence during probation) -- rung
    increments on each distinct escalation cycle. This is the chosen
    "already OPEN, re-occurrence" behavior: never re-dispatch WITHIN the
    cooldown window, but a recurrence genuinely AFTER it re-escalates."""
    _patch_ladder_gcs(monkeypatch)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    for i in range(3):
        ladder.record_occurrence("id-a", window_seconds=60.0, threshold=3, now=now + timedelta(seconds=i))
    state = ladder.get_state("id-a")
    assert state is not None
    assert state.rung == 1

    later = now + timedelta(seconds=200)  # well past the 60s cooldown
    transition = ladder.record_occurrence("id-a", window_seconds=60.0, threshold=3, now=later)
    assert transition == STATE_OPEN

    state = ladder.get_state("id-a")
    assert state is not None
    assert state.state == STATE_OPEN
    assert state.rung == 2
    assert state.last_escalated_at == later


def test_different_identities_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ladder_gcs(monkeypatch)
    for _ in range(3):
        ladder.record_occurrence("id-a", window_seconds=1800.0, threshold=3)
    assert ladder.record_occurrence("id-b", window_seconds=1800.0, threshold=3) == ""

    state_a = ladder.get_state("id-a")
    state_b = ladder.get_state("id-b")
    assert state_a is not None
    assert state_a.state == STATE_OPEN
    assert state_b is not None
    assert state_b.state == STATE_CLOSED


def test_threshold_of_one_escalates_on_first_occurrence(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ladder_gcs(monkeypatch)
    assert ladder.record_occurrence("id-a", window_seconds=1800.0, threshold=1) == STATE_OPEN


# ── durable persistence across a simulated "fresh process" ──────────────────


def test_state_survives_a_fresh_process_reinstantiation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write via one 'process' (one fake storage-client OBJECT), then read
    back via a SEPARATE fake storage-client object pointed at the same
    backing store -- simulating a restart where nothing survives in memory
    except the durable GCS bytes themselves (this module holds no
    module-level cache of its own, unlike circuit_breaker.py's in-process
    CircuitBreaker instance)."""
    shared_store: dict[tuple[str, str], bytes] = {}
    _patch_ladder_gcs(monkeypatch, store=shared_store)
    for _ in range(3):
        ladder.record_occurrence("id-restart", window_seconds=1800.0, threshold=3)

    first_state = ladder.get_state("id-restart")
    assert first_state is not None
    assert first_state.state == STATE_OPEN
    assert first_state.rung == 1

    # "Restart": a brand new fake client OBJECT never touched by the calls
    # above, seeded only with the same backing dict.
    _patch_ladder_gcs(monkeypatch, store=shared_store)
    restarted_state = ladder.get_state("id-restart")
    assert restarted_state is not None
    assert restarted_state.state == STATE_OPEN
    assert restarted_state.rung == 1
    assert restarted_state.occurrence_count == 3


def test_get_state_returns_none_for_unknown_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ladder_gcs(monkeypatch)
    assert ladder.get_state("never-seen") is None


# ── fail-open on unresolvable/broken durable state ───────────────────────────


def test_record_occurrence_returns_none_when_bucket_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ladder, "state_bucket", lambda: "")
    assert ladder.record_occurrence("id-a", window_seconds=1800.0) is None


def test_get_state_returns_none_when_bucket_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ladder, "state_bucket", lambda: "")
    assert ladder.get_state("id-a") is None


def test_record_occurrence_never_raises_on_storage_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ladder, "state_bucket", lambda: "alerting-service-test-proj")

    class _ExplodingClient:
        def download_bytes(self, bucket: str, blob_path: str) -> bytes:
            raise RuntimeError("gcs read exploded")

        def upload_bytes(self, *args: object, **kwargs: object) -> str:
            raise RuntimeError("gcs write exploded")

    monkeypatch.setattr(ladder, "get_storage_client", lambda: _ExplodingClient())
    # Read failure degrades to "no prior state" (bootstrap, per _read_state's
    # own never-raises contract); write failure is caught + logged inside
    # _write_state -- record_occurrence must still return the transition it
    # computed, never raise and never silently downgrade to None.
    assert ladder.record_occurrence("id-a", window_seconds=1800.0, threshold=1) == STATE_OPEN
