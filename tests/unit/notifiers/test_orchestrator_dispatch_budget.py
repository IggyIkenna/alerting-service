"""Unit tests for the ported GCS-durable dispatch-dedup checkpoint +
relaunch-dispatch budget (orchestrator_dispatch_budget.py, Phase 2 of
alerting_service_escalation_ladder_centralization_2026_08_18.md).

Mirrors deployment-service's tests/unit/test_escalation_dedup.py coverage
for check_dispatch_dedup_gcs / check_relaunch_dispatch_budget /
_shard_group_key / vm_prefix -- same scenarios, same fake-storage-client
shape, so behavioural parity with the ported original is directly checkable.
"""

from pathlib import Path

import pytest

from alerting_service.notifiers import orchestrator_dispatch_budget as budget

pytestmark = pytest.mark.unit


# ── vm_prefix / _shard_group_key ─────────────────────────────────────────────


def test_vm_prefix_falls_back_to_first_hyphen_segment_for_unregistered_name():
    assert budget.vm_prefix("totally-unregistered-vm-name-12345") == "totally"


def test_vm_prefix_matches_the_longer_registered_prefix_over_a_naive_first_segment():
    """The whole point of duplicating the registry prefix keys: 'cefi-aster-'
    (a real launcher-family entry) must win over the naive first-hyphen-
    segment guess of 'cefi' -- this is exactly the distinction
    _shard_group_key's grouping depends on for state-path continuity with
    deployment-service's original."""
    assert budget.vm_prefix("cefi-aster-2023-20260816-030139") == "cefi-aster-"
    assert budget.vm_prefix("cefi-extended-starknet-2026-20260816-040430") == "cefi-extended-"


def test_shard_group_key_extracts_bare_four_digit_year_after_prefix():
    assert budget._shard_group_key("cefi-aster-2023-20260816-030139", "cefi-aster-") == "cefi-aster-|2023"
    assert (
        budget._shard_group_key("cefi-extended-starknet-2026-20260816-040430", "cefi-extended-")
        == "cefi-extended-|2026"
    )


def test_shard_group_key_falls_back_to_bare_prefix_when_no_year_segment():
    assert budget._shard_group_key("cefi-instr-binance-20260816", "cefi-instr-") == "cefi-instr-"


# ── check_relaunch_dispatch_budget (local-only, tmp_path-scoped) ────────────


def test_check_relaunch_dispatch_budget_returns_none_for_empty_vm_name(tmp_path: Path):
    assert budget.check_relaunch_dispatch_budget(vm_name="", state_dir=tmp_path) is None


def test_check_relaunch_dispatch_budget_permits_first_two_then_bounds_the_third(tmp_path: Path):
    first = budget.check_relaunch_dispatch_budget(
        vm_name="features-sports-sports-2026-20260810-051126", state_dir=tmp_path
    )
    assert first is not None
    assert first["bounded"] is False
    assert first["dispatches_today"] == 1

    second = budget.check_relaunch_dispatch_budget(
        vm_name="features-sports-sports-2026-20260810-121107", state_dir=tmp_path
    )
    assert second is not None
    assert second["bounded"] is False
    assert second["dispatches_today"] == 2

    third = budget.check_relaunch_dispatch_budget(
        vm_name="features-sports-sports-2026-20260810-140033", state_dir=tmp_path
    )
    assert third is not None
    assert third["bounded"] is True
    assert third["dispatches_today"] == 2
    assert third["max_per_day"] == 2
    assert first["vm_prefix"] == second["vm_prefix"] == third["vm_prefix"]


def test_check_relaunch_dispatch_budget_rechecking_the_same_vm_never_double_counts(tmp_path: Path):
    kwargs = {"vm_name": "features-sports-sports-2026-20260810-051126", "state_dir": tmp_path}
    first = budget.check_relaunch_dispatch_budget(**kwargs)
    second = budget.check_relaunch_dispatch_budget(**kwargs)
    assert first is not None
    assert second is not None
    assert first["dispatches_today"] == second["dispatches_today"] == 1
    assert second["bounded"] is False


def test_check_relaunch_dispatch_budget_scoped_per_vm_prefix(tmp_path: Path):
    for i in range(2):
        result = budget.check_relaunch_dispatch_budget(
            vm_name=f"features-sports-sports-2026-2026081{i}-051126", state_dir=tmp_path
        )
        assert result is not None
        assert result["bounded"] is False

    bounded = budget.check_relaunch_dispatch_budget(
        vm_name="features-sports-sports-2026-20260812-051126", state_dir=tmp_path
    )
    assert bounded is not None
    assert bounded["bounded"] is True

    other_prefix = budget.check_relaunch_dispatch_budget(
        vm_name="cefi-backfill-coinbase-20260812-051126", state_dir=tmp_path
    )
    assert other_prefix is not None
    assert other_prefix["bounded"] is False


def test_check_relaunch_dispatch_budget_scopes_independently_per_shard_year(tmp_path: Path):
    """The bug this closes (2026-08-16, cefi_aster_relaunch_dispatch_budget_
    hit_2026_08_16.md): two DISTINCT VMs from the SAME shard-year (2023) hit
    the bound, but a third VM from a DIFFERENT shard-year (2024) under the
    identical launcher-family prefix must still dispatch normally."""
    for i in range(2):
        result = budget.check_relaunch_dispatch_budget(vm_name=f"cefi-aster-2023-2026081{i}-030139", state_dir=tmp_path)
        assert result is not None
        assert result["bounded"] is False

    bounded_2023 = budget.check_relaunch_dispatch_budget(vm_name="cefi-aster-2023-20260817-030139", state_dir=tmp_path)
    assert bounded_2023 is not None
    assert bounded_2023["bounded"] is True
    assert bounded_2023["shard_key"] == "cefi-aster-|2023"

    other_shard_year = budget.check_relaunch_dispatch_budget(
        vm_name="cefi-aster-2024-20260817-030139", state_dir=tmp_path
    )
    assert other_shard_year is not None
    assert other_shard_year["bounded"] is False
    assert other_shard_year["shard_key"] == "cefi-aster-|2024"
    assert other_shard_year["vm_prefix"] == bounded_2023["vm_prefix"] == "cefi-aster-"


def test_check_relaunch_dispatch_budget_never_raises_on_gcs_failure(monkeypatch):
    """Fail-open: when GCS bucket resolution explodes (no state_dir supplied,
    so local_only defaults False and the state object actually attempts the
    GCS path), the failure propagates up through _ShardedState.count()/claim()
    uncaught by those methods (their own try/excepts only guard the storage-
    client calls, not _state_bucket() itself) to check_relaunch_dispatch_
    budget's own outer try/except -- which degrades to None (permissive),
    never raises."""

    def _boom() -> str:
        raise RuntimeError("cloud config unavailable")

    # Patch the PUBLIC alias (`state_bucket`), not the private `_state_bucket`
    # -- `state_bucket = _state_bucket` binds a second name to the same
    # function OBJECT at module-load time, so patching `_state_bucket` alone
    # would not affect calls made through the `state_bucket` name (the
    # module-level call sites all use the public alias, mirroring
    # check_dispatch_dedup_gcs's own call site).
    monkeypatch.setattr(budget, "state_bucket", _boom)
    result = budget.check_relaunch_dispatch_budget(vm_name="cefi-aster-2023-20260818-000000")
    assert result is None


# ── check_dispatch_dedup_gcs (fake GCS storage client) ───────────────────────


class _FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], bytes] = {}

    def download_bytes(self, bucket: str, blob_path: str) -> bytes:
        key = (bucket, blob_path)
        if key not in self.store:
            raise FileNotFoundError(blob_path)
        return self.store[key]

    def upload_bytes(self, bucket: str, blob_path: str, data: bytes, content_type: str | None = None) -> str:
        self.store[(bucket, blob_path)] = data
        return f"gs://{bucket}/{blob_path}"


def _patch_gcs_checkpoint(monkeypatch, *, bucket: str = "deployment-scripts-test-proj") -> _FakeStorageClient:
    client = _FakeStorageClient()
    monkeypatch.setattr(budget, "state_bucket", lambda: bucket)
    monkeypatch.setattr(budget, "get_storage_client", lambda: client)
    return client


def test_check_dispatch_dedup_gcs_returns_none_when_bucket_unresolvable(monkeypatch):
    monkeypatch.setattr(budget, "state_bucket", lambda: "")
    result = budget.check_dispatch_dedup_gcs(
        asset_group="cefi",
        data_type="book_snapshot_5",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-16T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
    )
    assert result is None


def test_check_dispatch_dedup_gcs_bootstraps_first_dispatch_and_writes_checkpoint(monkeypatch):
    client = _patch_gcs_checkpoint(monkeypatch)
    result = budget.check_dispatch_dedup_gcs(
        asset_group="cefi",
        data_type="book_snapshot_5",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-16T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
    )
    assert result is not None
    assert result["skipped"] is False
    assert result["reason"] == "no_prior_checkpoint_bootstrap"
    assert len(client.store) == 1


def test_check_dispatch_dedup_gcs_identity_matches_the_uac_shared_function(monkeypatch):
    """The whole point of Phase 1: the checkpoint's GCS object key is exactly
    derive_escalation_identity()'s output, so deployment-service and
    alerting-service resolve the identical path for the identical finding."""
    from unified_api_contracts.alerting import derive_escalation_identity

    client = _patch_gcs_checkpoint(monkeypatch)
    budget.check_dispatch_dedup_gcs(
        asset_group="cefi",
        data_type="book_snapshot_5",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-16T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
    )
    identity = derive_escalation_identity(registry_id="DP-FETCH-009", asset_group="cefi", data_type="book_snapshot_5")
    expected_key = ("deployment-scripts-test-proj", f"vm-census/dispatch-dedup-checkpoint/{identity}.json")
    assert expected_key in client.store


def test_check_dispatch_dedup_gcs_skips_a_second_identical_static_backlog_reading(monkeypatch):
    _patch_gcs_checkpoint(monkeypatch)
    first = budget.check_dispatch_dedup_gcs(
        asset_group="cefi",
        data_type="book_snapshot_5",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-15T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
        is_static_backlog=True,
    )
    second = budget.check_dispatch_dedup_gcs(
        asset_group="cefi",
        data_type="book_snapshot_5",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-16T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
        is_static_backlog=True,
    )
    assert first is not None
    assert first["skipped"] is True
    assert first["reason"] == "static_backlog"
    assert second is not None
    assert second["skipped"] is True
    assert second["reason"] == "static_backlog"


def test_check_dispatch_dedup_gcs_dispatches_when_genuinely_new_activity(monkeypatch):
    _patch_gcs_checkpoint(monkeypatch)
    budget.check_dispatch_dedup_gcs(
        asset_group="cefi",
        data_type="derivative_ticker",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-15T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
    )
    second = budget.check_dispatch_dedup_gcs(
        asset_group="cefi",
        data_type="derivative_ticker",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-16T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
    )
    assert second is not None
    assert second["skipped"] is False
    assert second["reason"] == "new_activity_since_checkpoint"


def test_check_dispatch_dedup_gcs_skips_when_no_new_activity_since_checkpoint(monkeypatch):
    _patch_gcs_checkpoint(monkeypatch)
    budget.check_dispatch_dedup_gcs(
        asset_group="cefi",
        data_type="derivative_ticker",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-15T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
    )
    second = budget.check_dispatch_dedup_gcs(
        asset_group="cefi",
        data_type="derivative_ticker",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-15T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
    )
    assert second is not None
    assert second["skipped"] is True
    assert second["reason"] == "no_new_activity_since_checkpoint"


def test_check_dispatch_dedup_gcs_scopes_independently_per_tuple(monkeypatch):
    _patch_gcs_checkpoint(monkeypatch)
    budget.check_dispatch_dedup_gcs(
        asset_group="cefi",
        data_type="book_snapshot_5",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-15T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
        is_static_backlog=True,
    )
    other_tuple = budget.check_dispatch_dedup_gcs(
        asset_group="tradfi",
        data_type="ohlcv_15m",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-15T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
    )
    assert other_tuple is not None
    assert other_tuple["skipped"] is False


def test_check_dispatch_dedup_gcs_missing_tuple_returns_none(monkeypatch):
    _patch_gcs_checkpoint(monkeypatch)
    assert (
        budget.check_dispatch_dedup_gcs(
            asset_group="",
            data_type="book_snapshot_5",
            registry_id="DP-FETCH-009",
            max_attempted_at="2026-08-15T00:00:00Z",
            event="DP_RUN_MOSTLY_EMPTY",
        )
        is None
    )


def test_check_dispatch_dedup_gcs_never_raises_on_storage_exception(monkeypatch):
    """A GCS read/write failure inside the private read/write helpers is
    already caught THERE (never-raises, degrades to "no prior checkpoint" /
    a silently-failed write) -- ported verbatim from the original, so the
    overall call still resolves a normal bootstrap verdict rather than
    exploding through to the outer handler. Fixed-vocabulary vs. an
    unexpected raise is the property under test: this must not raise."""
    monkeypatch.setattr(budget, "state_bucket", lambda: "deployment-scripts-test-proj")

    class _ExplodingClient:
        def download_bytes(self, bucket: str, blob_path: str) -> bytes:
            raise RuntimeError("gcs read exploded")

        def upload_bytes(self, *args: object, **kwargs: object) -> str:
            raise RuntimeError("gcs write exploded")

    monkeypatch.setattr(budget, "get_storage_client", lambda: _ExplodingClient())
    result = budget.check_dispatch_dedup_gcs(
        asset_group="cefi",
        data_type="book_snapshot_5",
        registry_id="DP-FETCH-009",
        max_attempted_at="2026-08-16T00:00:00Z",
        event="DP_RUN_MOSTLY_EMPTY",
    )
    # Never raised; the inner read/write failures degrade to a bootstrap
    # verdict (checkpoint unreadable -> treated as absent -> dispatch once).
    assert result is not None
    assert result["skipped"] is False
    assert result["reason"] == "no_prior_checkpoint_bootstrap"
