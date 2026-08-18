"""GCS-durable dispatch-dedup checkpoint + relaunch-dispatch budget gating the
relocated GitHub-dispatch call (``orchestrator_dispatch.py``, sibling module).

Phase 2 of
``unified-trading-pm/plans/active/alerting_service_escalation_ladder_centralization_2026_08_18.md``
("Centralize disaster-recovery escalation-ladder decisions in
alerting-service"). Ported from deployment-service's
``deployment_service/data_pipeline_monitors/escalation_dedup.py`` -- the
GCS-checkpoint variant ONLY (``check_dispatch_dedup_gcs``,
``check_relaunch_dispatch_budget``), NOT ``check_dispatch_dedup`` /
``check_dispatch_dedup_vm``, which read a local ``unified-trading-pm`` clone
on disk -- alerting-service is a Cloud Run SERVICE with no local PM clone at
all, so only the GCS-durable variant is portable here.

State continuity across the cutover
------------------------------------
Both ported checks read/write the SAME ``deployment-scripts-<project>``
bucket and the SAME ``vm-census/dispatch-dedup-checkpoint/`` /
``vm-census/relaunch-dispatch-budget/`` GCS prefixes the deployment-service
originals already use, so a checkpoint/budget object written by
deployment-service BEFORE this migration is read correctly here (verified
live -- see the plan's Progress Log) rather than reset to zero.

Identity: the shared UAC function, never a local re-derivation
------------------------------------------------------------------
The dispatch-dedup checkpoint's identity key comes from
``unified_api_contracts.alerting.derive_escalation_identity`` (this plan's
Phase 1) -- its tuple-keyed branch is verified byte-identical to
``escalation_dedup.py``'s own ``_dispatch_checkpoint_identity()``, so the two
repos can never derive two different keys for the same finding.

``vm_prefix`` / shard-year grouping -- a deliberately-scoped point-in-time copy
---------------------------------------------------------------------------------
``check_relaunch_dispatch_budget``'s launcher-family grouping needs a
LONGEST-PREFIX match against deployment-service's
``deployment_service.vm_prefix_registry.VM_PREFIX_TO_BUCKET`` registry
(~250 entries) to reproduce the SAME ``shard_key`` deployment-service used to
write budget objects under. Getting this wrong would make alerting-service
read/write a DIFFERENT GCS path than deployment-service's history for any VM
whose true registry prefix is longer than its naive first-hyphen segment --
the common case (e.g. the registry's ``"cefi-aster-"`` vs a naive
first-segment guess of ``"cefi"``) -- silently breaking state continuity and
the whole point of the per-launcher-family budget (see
``_shard_group_key``'s own history, cefi_aster_relaunch_dispatch_budget_hit_
2026_08_16.md).

``derive_escalation_identity`` does not cover this shape -- launcher-fleet
grouping is not a per-finding identity concept -- and alerting-service
(Tier 4) cannot import ``deployment_service.vm_prefix_registry`` directly:
"NO service<->service deps" (T4 depends only on UTL/UAC/
``unified-*-interface``; ``/codex/04-architecture/tier-and-import-architecture.md``).
So ``_VM_PREFIX_KEYS`` below is a READ-ONLY, port-time (2026-08-18) copy of
that registry's dict KEYS ONLY (never the ``VmPrefixSpec`` bucket/lifecycle
VALUES, which this module has no use for) -- the same kind of intra-repo
duplication ``escalation_dedup.py``'s own ``vm_prefix()`` docstring already
documents as an accepted pattern in this codebase (its own copy rather than
importing a module that is not safely importable from where it runs).

KNOWN LIMITATION, flagged for follow-up (not fixed here -- out of this
phase's scope): this list will drift as deployment-service's registry grows
a new launcher prefix after the port date. A candidate follow-up is
promoting the prefix-key list to a shared UAC registry both repos import.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from unified_api_contracts.alerting import derive_escalation_identity
from unified_trading_library import UnifiedCloudConfig, get_storage_client

logger = logging.getLogger(__name__)


def _state_bucket() -> str:
    """Durable state bucket ``deployment-scripts-<project>``; ``""`` when
    unresolvable. Mirrors ``deployment-service/scripts/recovery/
    _durable_state.py::_state_bucket()`` exactly, including its NOT routing
    through ``resolve_bucket_name()`` -- this pre-existing convention is
    reused as-is for state continuity (the same "Aside, not in scope" call
    this plan already made for ``storage_store.py::_bucket_name()``), not
    fixed here.
    """
    try:
        cfg = UnifiedCloudConfig()
    except (ValueError, RuntimeError, OSError):
        return ""
    proj = str(getattr(cfg, "gcp_project_id", "") or "")
    return f"deployment-scripts-{proj}" if proj else ""


# Public alias -- mirrors _durable_state.py's own `state_bucket = _state_bucket`
# convention (a documented test-patch target for callers/tests).
state_bucket = _state_bucket


# ── GCS-durable dispatch-dedup checkpoint ────────────────────────────────────
_DISPATCH_CHECKPOINT_STATE_ROOT = "vm-census/dispatch-dedup-checkpoint"


def _dispatch_checkpoint_blob_path(identity: str) -> str:
    return f"{_DISPATCH_CHECKPOINT_STATE_ROOT}/{identity}.json"


def _read_dispatch_checkpoint(bucket: str, identity: str) -> dict[str, object] | None:
    try:
        raw = get_storage_client().download_bytes(bucket, _dispatch_checkpoint_blob_path(identity))
    except Exception:  # noqa: broad-except — checkpoint read must degrade to "absent", never raise
        return None
    try:
        loaded = cast("object", json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        return None
    return cast("dict[str, object]", loaded) if isinstance(loaded, dict) else None


def _write_dispatch_checkpoint(bucket: str, identity: str, *, max_attempted_at: str, is_static_backlog: bool) -> bool:
    payload = json.dumps(
        {
            "max_attempted_at": max_attempted_at,
            "is_static_backlog": is_static_backlog,
            "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    ).encode("utf-8")
    try:
        get_storage_client().upload_bytes(
            bucket, _dispatch_checkpoint_blob_path(identity), payload, content_type="application/json"
        )
        return True
    except Exception as exc:  # noqa: broad-except — checkpoint write is best-effort, must never raise
        logger.warning("dispatch checkpoint: write failed for %s: %s", identity, exc)
        return False


def check_dispatch_dedup_gcs(
    *,
    asset_group: str,
    data_type: str,
    registry_id: str,
    max_attempted_at: str,
    event: str,
    is_static_backlog: bool = False,
) -> dict[str, object] | None:
    """GCS-durable dedup for the relocated orchestrator-dispatch call.

    Ported verbatim from ``escalation_dedup.check_dispatch_dedup_gcs`` --
    only the identity derivation changed (``derive_escalation_identity``
    instead of the local ``_dispatch_checkpoint_identity`` helper; verified
    byte-identical for this tuple shape, see Phase 1 of the migration plan).

    Returns ``None`` when the state bucket can't be resolved (fail-open: the
    caller's original always-dispatch behaviour is unaffected) or the tuple
    is unresolvable. Never raises.
    """
    if not asset_group or not data_type:
        return None
    bucket = state_bucket()
    if not bucket:
        return None
    identity = derive_escalation_identity(registry_id=registry_id, asset_group=asset_group, data_type=data_type)
    try:
        checkpoint = _read_dispatch_checkpoint(bucket, identity)
        # is_static_backlog wins even over a bootstrap (no prior checkpoint) --
        # mirrors the original's precedence: the alert's own materiality
        # classification already says this reading isn't dedup-worthy new
        # activity, so there is no reason to burn even the first full
        # dispatch waiting to observe a second identical reading.
        if is_static_backlog:
            has_new_activity, reason = False, "static_backlog"
        elif checkpoint is None:
            has_new_activity, reason = True, "no_prior_checkpoint_bootstrap"
        elif not max_attempted_at:
            has_new_activity, reason = True, "max_attempted_at_unresolvable"
        else:
            prior = str(
                checkpoint.get("max_attempted_at", "")  # noqa: qg-empty-fallback — absent checkpoint field means unset
            ).strip()
            has_new_activity = (not prior) or (max_attempted_at > prior)
            reason = "new_activity_since_checkpoint" if has_new_activity else "no_new_activity_since_checkpoint"
        _write_dispatch_checkpoint(
            bucket, identity, max_attempted_at=max_attempted_at, is_static_backlog=is_static_backlog
        )
        return {"skipped": not has_new_activity, "checkpoint_key": identity, "reason": reason}
    except Exception as exc:  # noqa: broad-except — dedup must never break the escalation hop
        logger.warning("gcs dispatch checkpoint dedup: check failed (falling through to full dispatch): %s", exc)
        return None


# ── relaunch-dispatch budget (<=2 DISTINCT VMs per (launcher-family, shard-
#    year), per day) ──────────────────────────────────────────────────────
_RELAUNCH_DISPATCH_STATE_ROOT = "vm-census/relaunch-dispatch-budget"
_MAX_RELAUNCH_DISPATCHES_PER_DAY: int = 2

# Matches a bare 4-digit year token (2000-2099) at a hyphen boundary -- ported
# verbatim from escalation_dedup.py's _SHARD_YEAR_RE (see its comment for why
# it deliberately does not match inside an 8-digit launch-date or 6-digit
# time segment).
_SHARD_YEAR_RE = re.compile(r"(?:^|-)(20\d{2})(?:-|$)")


def _shard_group_key(vm_name: str, prefix: str) -> str:
    """``<prefix>|<shard-year>`` when ``vm_name`` carries an unambiguous bare
    4-digit shard-year segment after its launcher-family prefix, else just
    ``prefix``. Ported verbatim from ``escalation_dedup._shard_group_key``.
    """
    remainder = vm_name[len(prefix) :] if vm_name.startswith(prefix) else vm_name
    match = _SHARD_YEAR_RE.search(remainder)
    if match:
        return f"{prefix}|{match.group(1)}"
    return prefix


# Point-in-time (2026-08-18) copy of deployment-service's
# `deployment_service.vm_prefix_registry.VM_PREFIX_TO_BUCKET` dict KEYS only
# -- see the module docstring's "vm_prefix / shard-year grouping" section for
# why this duplication exists and its known drift limitation. Extracted
# programmatically from that registry at port time, not hand-transcribed.
_VM_PREFIX_KEYS: frozenset[str] = frozenset(
    {
        "cefi-mr-",
        "cefi-fwd-",
        "cefi-binance-",
        "cefi-bybit-",
        "cefi-deribit-",
        "cefi-coinbase-",
        "cefi-okx-",
        "cefi-upbit-",
        "cefi-hyperliquid-",
        "cefi-bitfinex-",
        "cefi-bitget-",
        "cefi-kraken-",
        "cefi-aster-",
        "cefi-cme-",
        "cefi-extended-",
        "cefi-ext-bfill-",
        "cefi-lighter-",
        "cefi-queue-",
        "aster-fwd-",
        "defi-fwd-",
        "prediction-fwd-",
        "prediction-live-",
        "prediction-arb-detector-",
        "betfair-egress-proxy-",
        "cefi-instr-",
        "cefi-rogue-",
        "instr-backfill-cefi-",
        "instr-backfill-defi",
        "instr-backfill-tradfi",
        "instr-backfill-sports",
        "cefi-durability-force-converge-",
        "instr-backfill-pred",
        "fss-backfill-vm-",
        "features-sfi-progressive-",
        "sports-ref-v3-",
        "footystats-fwd-",
        "sfi-fwd-",
        "sports-manifest-rescan-",
        "tradfi-bf-",
        "tradfi-bf-fred-",
        "tradfi-bf-cme-ohlcv-1m-",
        "tradfi-bf-ice-ohlcv-1m-",
        "tradfi-bf-nasdaq-ohlcv-1m-",
        "tradfi-bf-nyse-ohlcv-1m-",
        "tradfi-bf-cboe-ohlcv-1m-",
        "tradfi-bf-cfe-ohlcv-1m-",
        "tradfi-bf-fx-ohlcv-24h-",
        "tradfi-bf-krx-eq-ohlcv-24h-",
        "tradfi-bf-cboe-idx-ohlcv-24h-",
        "tradfi-bf-ice-idx-ohlcv-24h-",
        "tradfi-fwd-",
        "tradfi-recent-",
        "tradfi-event-contract-backfill-",
        "tradfi-instr-",
        "tradfi-phantom-audit",
        "mdps-cefi-",
        "mdps-cefi-manifest-merge-",
        "mdps-tradfi-",
        "mdps-defi-",
        "mdps-prediction-",
        "mdps-sports-",
        "mdps-backfill-cefi-",
        "mdps-backfill-tradfi-",
        "mdps-backfill-defi-",
        "mdps-backfill-prediction-",
        "mdps-backfill-sports-",
        "mtds-prediction-",
        "mtds-perp-funding-",
        "mtds-gas-fees-",
        "mtds-lst-rates-",
        "mtds-vault-",
        "mtds-lending-indices-",
        "mtds-pyth-archive-",
        "governance-backfill-",
        "pyth-lst-backfill-",
        "jito-solana-backfill-",
        "marinade-backfill-",
        "strategy-backtest-grid-",
        "strategy-test-",
        "ml-",
        "exec-alpha-",
        "strategy-paper-",
        "greeks-compute-live-",
        "greeks-compute-batch-",
        "defi-manifest-projection-",
        "defi-manifest-force-consolidate-",
        "defi-backtest-",
        "defi-paper-",
        "funding-ensemble-paper-",
        "strategy-live-",
        "defi-recursive-",
        "deployment-dashboard-vm",
        "alerting-quietness-",
        "synbench-",
        "mtds-liquidations-backfill",
        "prediction-features-",
        "mtds-gas-fees-solana",
        "sports-full-sweep-",
        "sports-entity-",
        "prediction-pipeline-",
        "mtds-dex-pools-backfill",
        "mtds-dex-swaps-backfill",
        "mtds-dex-pools-",
        "mtds-dex-swaps-",
        "mtds-liquidations-",
        "mtds-position-data-",
        "mtds-liquidation-events-",
        "mtds-flash-loan-events-",
        "mtds-bridge-events-",
        "mtds-risk-params-",
        "mtds-eigenlayer-rewards-backfill",
        "mtds-oracle-prices-backfill",
        "mtds-solana-defi-backfill",
        "mtds-migrate-",
        "mtds-backfill-cefi-",
        "mtds-backfill-tradfi-",
        "mtds-backfill-defi-",
        "mtds-backfill-prediction-",
        "mtds-backfill-sports-",
        "mtds-backfill-odds-",
        "defi-phantom-recon-",
        "manifest-recon-",
        "manifest-recon-apply-cefi-",
        "manifest-recon-apply-defi-",
        "manifest-recon-apply-tradfi-",
        "datapoint-validation-cefi-",
        "datapoint-validation-defi-",
        "datapoint-validation-tradfi-",
        "datapoint-validation-sports-",
        "datapoint-validation-prediction-",
        "orphan-sweep-cefi-",
        "orphan-sweep-defi-",
        "orphan-sweep-tradfi-",
        "orphan-sweep-prediction-",
        "sports-schema-census-instruments-store-",
        "sports-schema-census-features-sports-",
        "feat-orph-",
        "ml-orph-",
        "strat-orph-",
        "feat-orph-bf-",
        "sports-derived-features-census-",
        "backfill-orphan-e-cefi-",
        "backfill-orphan-e-defi-",
        "backfill-orphan-e-tradfi-",
        "backfill-orphan-e-prediction-",
        "backfill-candle-manifest-cefi-",
        "backfill-candle-manifest-defi-",
        "backfill-candle-manifest-tradfi-",
        "backfill-candle-manifest-prediction-",
        "backfill-defi-dex-swaps-",
        "backfill-defi-legacy-datatype-fold-",
        "gcs-migration-phase0-",
        "batch-live-recon-",
        "expected-universe-v2-",
        "blank-reason-recon-",
        "blank-reason-recon-cefi-",
        "blank-reason-recon-defi-",
        "blank-reason-recon-tradfi-",
        "blank-reason-recon-sports-",
        "blank-reason-recon-prediction-",
        "opt-deribit-",
        "deribit-opts-fwd-",
        "dvol-deribit-",
        "opt-okx-",
        "opt-cboe-",
        "opt-cme-",
        "cme-events-",
        "fs-backfill-",
        "fts-backfill-",
        "af-backfill-",
        "af-audit-",
        "af-recover-",
        "tm-backfill-",
        "tm-forward-poll-",
        "sfi-backfill-",
        "us-backfill-",
        "us-forward-poll-",
        "weather-backfill-",
        "fill-missing-player-stats-",
        "features-",
        "manifest-consolidator-",
        "data-status-rollup-",
        "sports-scheduler-",
        "tier3-audit-",
        "reconcile-phantom-",
        "cross-asset-rescan-",
        "measure-honest-coverage-",
        "tradfi-audit-aggregate-",
        "instr-",
        "instruments-smoke-",
        "combo-migration-",
        "canonical-migration-cefi-",
        "canonical-migration-tradfi-",
        "canonical-migration-defi-",
        "canonical-migration-prediction-",
        "canonical-migration-sports-",
        "canonical-migration-sports-features-",
        "canonical-migration-cefi-fts-",
        "canonical-migration-cefi-fts-ext-",
        "sports-v9-migration-",
        "mdps-sports-bucket-",
        "mtds-live-cefi-consolidated-",
        "mtds-live-cefi-",
        "mtds-live-defi-",
        "mtds-live-tradfi-",
        "mtds-live-sports-",
        "mtds-live-prediction-",
        "mtds-live-smoke-",
        "pipeline-e2e-check-",
        "mdps-features-live-cefi-",
        "mdps-features-live-defi-",
        "mdps-features-live-tradfi-",
        "mdps-features-live-sports-",
        "mdps-features-live-prediction-",
        "features-xc-",
        "replay-",
        "disaster-drill-cron-",
        "dr-drill-cutover-",
        "live-strategy-",
        "live-execution-",
        "live-mtds-",
        "live-pbm-",
        "live-risk-",
        "live-alerting-",
        "exp-ml-",
        "exp-strategy-",
        "exp-execution-",
        "aave-lending-rate-val-",
        "amm-golden-",
        "wallet-treasury-cutover-",
        "client-reporting-cutover-",
        "qg-snapshot-",
        "batch-live-smoke-matrix-",
        "tradfi-fwd-daily-cron-",
        "cefi-fwd-daily-cron-",
        "cefi-onchain-fwd-daily-cron-",
        "cefi-perp-funding-daily-cron-",
        "funding-ensemble-daily-cron-",
        "bucket-rsync-",
        "vm-zombie-watchdog-",
        "dm-",
        "scenario-matrix-",
        "agent-orch-planning-vm-",
        "expected-universe-v2-sports-",
        "datapoint-validation-",
        "cefi-onchain-fwd-",
    }
)


def vm_prefix(vm_name: str) -> str:
    """Longest-prefix match against ``_VM_PREFIX_KEYS`` (the launcher-family
    registry), falling back to the VM name's first hyphen segment.

    Mirrors ``escalation_dedup.vm_prefix`` / ``scripts.recovery.
    relaunch_backfill_vm.vm_prefix`` exactly.
    """
    best: str | None = None
    for candidate in _VM_PREFIX_KEYS:
        if vm_name.startswith(candidate) and (best is None or len(candidate) > len(best)):
            best = candidate
    if best is not None:
        return best
    return vm_name.split("-", 1)[0] if "-" in vm_name else vm_name


def _default_relaunch_dispatch_state_dir() -> Path:
    return Path(tempfile.gettempdir()) / "uts_relaunch_dispatch_budget"


class _ShardedState:
    """Race-free durable state: ONE OBJECT PER FACT, never a shared mutable doc.

    Own copy of ``deployment-service/scripts/recovery/_durable_state.
    ShardedState`` (that module lives in deployment-service's unpackaged
    ``scripts/`` tree, not importable cross-repo -- and Phase 3 of this same
    plan independently rules alerting-service's escalation-ladder state gets
    its OWN implementation rather than a cross-repo import of this class, for
    the identical reason). Trimmed to the two methods
    ``check_relaunch_dispatch_budget`` actually calls (``claim`` / ``count``)
    -- see the original for the full ``exists()``-bearing version and the
    fleet-load rationale for why this is atomic create-if-absent + object-count
    rather than a shared read-modify-write JSON doc.
    """

    def __init__(self, root: str, *, local_dir: Path, local_only: bool = False) -> None:
        self._root = root
        self._local_dir = local_dir
        self._local_only = local_only

    def _key(self, group: str, name: str) -> str:
        return f"{self._root}/{group}/{name}.json"

    def _local_file(self, group: str, name: str) -> Path:
        return self._local_dir / self._root.replace("/", "_") / group / f"{name}.json"

    def claim(self, group: str, name: str, payload: str = "") -> bool:
        """Atomically record one fact. ``True`` iff THIS caller created it."""
        data = (payload or name).encode("utf-8")
        if not self._local_only:
            bucket = state_bucket()
            if bucket:
                try:
                    gen = get_storage_client().conditional_upload_bytes(
                        bucket, self._key(group, name), data, if_generation_match=0, content_type="application/json"
                    )
                except Exception as exc:  # noqa: broad-except — durable claim degrades to the local mirror, never raises
                    logger.warning("durable claim failed for %s/%s: %s", group, name, exc)
                else:
                    return gen is not None
        path = self._local_file(group, name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # "x" is the POSIX create-if-absent analogue of if_generation_match=0.
            with path.open("x", encoding="utf-8") as fh:
                _ = fh.write(payload or name)
        except FileExistsError:
            return False
        except OSError as exc:
            logger.warning("local claim mirror failed for %s/%s: %s", group, name, exc)
            return True  # fail-open: permit the action rather than silently drop it
        return True

    def count(self, group: str) -> int:
        """How many facts are recorded under ``group`` (objects, not a counter)."""
        if not self._local_only:
            bucket = state_bucket()
            if bucket:
                try:
                    return sum(1 for _ in get_storage_client().list_blobs(bucket, prefix=f"{self._root}/{group}/"))
                except Exception:  # noqa: broad-except — count degrades to the local mirror, never raises
                    pass
        parent = self._local_file(group, "_").parent
        try:
            return sum(1 for p in parent.iterdir() if p.suffix == ".json")
        except OSError:
            return 0


def check_relaunch_dispatch_budget(
    *,
    vm_name: str,
    now: datetime | None = None,
    state_dir: Path | None = None,
    local_only: bool = False,
) -> dict[str, object] | None:
    """<=``_MAX_RELAUNCH_DISPATCHES_PER_DAY`` DISTINCT VMs of one
    ``(launcher-family, shard-year)`` group get a RELAUNCH instruction
    dispatched per calendar day. Ported verbatim from
    ``escalation_dedup.check_relaunch_dispatch_budget`` (same state root,
    same day-partition key format, same bound) -- see the module docstring
    for why the grouping key is finer than the launcher-family prefix alone.

    Returns ``None`` when ``vm_name`` is empty (nothing to bound) or the
    check itself fails (GCS unreachable, etc.) -- never raises, and a
    failure degrades to PERMISSIVE (the caller's original
    always-instruct-relaunch behaviour), the same fail-open direction every
    other durable-state actuator here takes. Otherwise stamps this
    ``vm_name`` (idempotent -- :meth:`_ShardedState.claim` is
    create-if-absent, so re-checking the SAME vm_name never double-counts)
    and returns ``{"bounded": bool, "vm_prefix": str, "shard_key": str,
    "dispatches_today": int, "max_per_day": int}``.

    ``state_dir``/``local_only`` mirror the original's test-injection points
    -- tests inject a ``tmp_path`` so a test run never reads/writes the
    shared host tempdir or a real GCS bucket.
    """
    if not vm_name:
        return None
    try:
        prefix = vm_prefix(vm_name)
        shard_key = _shard_group_key(vm_name, prefix)
        state = _ShardedState(
            _RELAUNCH_DISPATCH_STATE_ROOT,
            local_dir=state_dir or _default_relaunch_dispatch_state_dir(),
            local_only=local_only or state_dir is not None,
        )
        day_key = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
        group = f"{day_key}/{shard_key}"
        count_before = state.count(group)
        if count_before >= _MAX_RELAUNCH_DISPATCHES_PER_DAY:
            return {
                "bounded": True,
                "vm_prefix": prefix,
                "shard_key": shard_key,
                "dispatches_today": count_before,
                "max_per_day": _MAX_RELAUNCH_DISPATCHES_PER_DAY,
            }
        # claim() is create-if-absent: re-checking the SAME vm_name (e.g. a
        # retried dispatch) returns False and must NOT report an incremented
        # count -- the object count in storage didn't actually change.
        claimed = state.claim(group, vm_name)
        return {
            "bounded": False,
            "vm_prefix": prefix,
            "shard_key": shard_key,
            "dispatches_today": count_before + 1 if claimed else count_before,
            "max_per_day": _MAX_RELAUNCH_DISPATCHES_PER_DAY,
        }
    except Exception as exc:  # noqa: broad-except — budget check must never break the escalation hop
        logger.warning("relaunch dispatch budget: check failed (falling through to permissive): %s", exc)
        return None
