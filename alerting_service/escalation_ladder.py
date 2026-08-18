"""GCS-durable escalation ladder — the CLOSED/OPEN/HALF_OPEN retry-counter
gating the relocated GitHub-dispatch call (``notifiers/orchestrator_dispatch.py``
/ ``notifiers/orchestrator_dispatch_gate.py``) for FILE_ISSUE/PAGE_OPERATOR-tier
data-pipeline findings.

Phase 3 of
``unified-trading-pm/plans/active/alerting_service_escalation_ladder_centralization_2026_08_18.md``
("Centralize disaster-recovery escalation-ladder decisions in
alerting-service"). Modeled on ``alerting_service/circuit_breaker.py``'s
CLOSED/OPEN/HALF_OPEN state-name and transition-shape conventions (CLOSED =
below the escalation threshold, OPEN = escalated, HALF_OPEN = probation
after a cooldown) but backed by DURABLE per-identity state instead of that
module's in-process ``dict``s. This is its own implementation — NOT a
cross-repo import of deployment-service's
``scripts/recovery/_durable_state.ShardedState`` (that module lives in that
repo's unpackaged ``scripts/`` tree, and alerting-service is Tier 4: "no
service<->service deps",
``/codex/04-architecture/tier-and-import-architecture.md``).

State lives under alerting-service's OWN bucket (``alerting-service-<project>``,
mirroring ``persistence/storage_store.py``'s existing ``_bucket_name()`` /
``_get_cloud_config()`` convention exactly — same derivation, duplicated
locally rather than imported, since both names are module-private there and
basedpyright's ``reportPrivateUsage`` flags a cross-module private import
even within the same repo; this is the identical "reuse the convention, not
a fresh derivation" call the plan's own docstring already makes for this
bucket), one JSON object per identity, under
``alerting/state/escalation-ladder/<identity>.json`` — sibling
to the existing ``alerting/state/cooldowns.json``. Identity comes from Phase
1's shared ``unified_api_contracts.alerting.derive_escalation_identity`` so
deployment-service (Phase 4's reader) resolves the SAME object for the SAME
finding.

Genuine "try N occurrences quietly, the Nth crossing escalates" ladder: a
recurring alert's per-event Slack mirror (``_mirror_to_data_pipeline_slack`` /
``_mirror_to_uts_live_alerts_slack_dp`` in ``notifiers/router.py``) already
fires on EVERY routed occurrence regardless of ladder state — this module
governs only the LOUD signal (the relocated GitHub ``repository_dispatch``
fast-spawn), never visibility.

Return-value contract — mirrors circuit_breaker.py's with ONE necessary
extension
------------------------------------------------------------------------
:func:`record_occurrence` returns the same shape as
``circuit_breaker.CircuitBreaker.record_error``/``record_success``: the new
state name string on a transition (``STATE_OPEN``), or ``""`` when no
transition happened. circuit_breaker.py's pure in-process design never needed
a third outcome — a durable, network-backed store can fail to read/write, and
this module extends the contract with ``None`` for exactly that case (the
durable operation itself could not be performed: bucket unresolvable, GCS
exception). Callers MUST treat ``None`` the same as every other durable-state
check this plan's Phase 2 already shipped (``orchestrator_dispatch_budget.
check_dispatch_dedup_gcs`` / ``check_relaunch_dispatch_budget``): fail OPEN
toward escalating, never fail toward silently swallowing a
FILE_ISSUE/PAGE_OPERATOR-tier finding forever — the PAGE_OPERATOR tier exists
specifically for conditions that need a human, so a ladder outage must never
be the reason one never gets escalated.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import cast

from unified_trading_library import UnifiedCloudConfig, get_storage_client

from alerting_service.circuit_breaker import STATE_CLOSED, STATE_HALF_OPEN, STATE_OPEN

logger = logging.getLogger(__name__)

_STATE_PREFIX = "alerting/state/escalation-ladder"
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Plan-mandated starting threshold (Phase 3, not a "tune this later"
# placeholder): 3 occurrences of a FILE_ISSUE/PAGE_OPERATOR-tier finding
# within the caller-supplied window escalate to the loud GitHub-dispatch
# path. Occurrences 1-2 stay quiet (Slack-mirror only, which already fires
# unconditionally regardless of ladder state); the 3rd crossing dispatches.
DEFAULT_ESCALATION_THRESHOLD = 3


def _fmt(moment: datetime) -> str:
    return moment.strftime(_TS_FORMAT)


def _parse(raw: str) -> datetime:
    return datetime.strptime(raw, _TS_FORMAT).replace(tzinfo=UTC)


@dataclass
class LadderState:  # CORRECT-LOCAL — service-internal durable-state record (this module's own
    # GCS JSON schema), not a cross-service domain contract; deployment-service's Phase 4 reader
    # reads the raw JSON object directly (see the module docstring), never this Python type.
    """One identity's durable escalation-ladder record.

    Fields mirror the plan's Phase 3 todo verbatim: occurrence count,
    first-seen timestamp, current rung, and last-escalated-at — plus
    ``state`` (CLOSED/OPEN/HALF_OPEN, reusing circuit_breaker.py's own
    constants) and ``identity`` (the GCS object-key stem, carried on the
    record so a caller holding only a ``LadderState`` can still identify it).
    """

    identity: str
    state: str
    occurrence_count: int
    first_seen_at: datetime
    last_occurrence_at: datetime
    rung: int
    last_escalated_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "state": self.state,
            "occurrence_count": self.occurrence_count,
            "first_seen_at": _fmt(self.first_seen_at),
            "last_occurrence_at": _fmt(self.last_occurrence_at),
            "rung": self.rung,
            "last_escalated_at": _fmt(self.last_escalated_at) if self.last_escalated_at is not None else None,
        }

    @staticmethod
    def from_dict(raw: dict[str, object]) -> LadderState:
        last_escalated_raw = raw.get("last_escalated_at")
        return LadderState(
            identity=str(raw["identity"]),
            state=str(raw["state"]),
            occurrence_count=int(cast("int", raw["occurrence_count"])),
            first_seen_at=_parse(str(raw["first_seen_at"])),
            last_occurrence_at=_parse(str(raw["last_occurrence_at"])),
            rung=int(cast("int", raw["rung"])),
            last_escalated_at=_parse(str(last_escalated_raw)) if last_escalated_raw is not None else None,
        )


@lru_cache(maxsize=1)
def _get_cloud_config() -> UnifiedCloudConfig:
    """Singleton ``UnifiedCloudConfig`` — a local copy of
    ``persistence/storage_store.py``'s own ``_get_cloud_config()`` (not
    imported: see the module docstring's "State lives under..." paragraph
    for why this is duplicated rather than cross-module-imported).
    """
    return UnifiedCloudConfig()


def _bucket_name(project_id: str) -> str:
    """Convention: ``alerting-service-{project_id}`` — identical derivation
    to ``persistence/storage_store.py::_bucket_name()``."""
    return f"alerting-service-{project_id}"


def _state_bucket() -> str:
    """Escalation-ladder durable-state bucket: alerting-service's own
    (``alerting-service-<project>``). Empty string when unresolvable — the
    fail-open caller pattern documented on :func:`record_occurrence`.
    """
    try:
        project_id = str(_get_cloud_config().gcp_project_id or "")
    except (ValueError, RuntimeError, OSError):
        return ""
    return _bucket_name(project_id) if project_id else ""


# Public alias — mirrors orchestrator_dispatch_budget.py's own
# `state_bucket = _state_bucket` convention (a documented test-patch target;
# callers/tests patch THIS name, not the private one).
state_bucket = _state_bucket


def _blob_path(identity: str) -> str:
    return f"{_STATE_PREFIX}/{identity}.json"


def _read_state(bucket: str, identity: str) -> LadderState | None:
    try:
        raw = get_storage_client().download_bytes(bucket, _blob_path(identity))
    except Exception:  # noqa: broad-except — read degrades to "no prior state", never raises
        return None
    try:
        loaded = cast("object", json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    try:
        return LadderState.from_dict(cast("dict[str, object]", loaded))
    except (KeyError, ValueError, TypeError):
        return None


def _write_state(bucket: str, state: LadderState) -> bool:
    payload = json.dumps(state.to_dict()).encode("utf-8")
    try:
        get_storage_client().upload_bytes(bucket, _blob_path(state.identity), payload, content_type="application/json")
        return True
    except Exception as exc:  # noqa: broad-except — write is best-effort, must never raise
        logger.warning("escalation ladder: state write failed for %s: %s", state.identity, exc)
        return False


def _bootstrap_state(identity: str, now: datetime) -> LadderState:
    return LadderState(
        identity=identity,
        state=STATE_CLOSED,
        occurrence_count=0,
        first_seen_at=now,
        last_occurrence_at=now,
        rung=0,
        last_escalated_at=None,
    )


def _advance_state(state: LadderState, *, now: datetime, window_seconds: float, threshold: int) -> str:
    """Mutate ``state`` in place for one new occurrence at ``now``; return
    the circuit_breaker-style transition string (``""`` / ``STATE_OPEN``).

    State machine (identity-scoped, durable across process restarts):

        CLOSED    -- ``threshold`` occurrences inside ``window_seconds`` --> OPEN
        OPEN      -- another occurrence before ``window_seconds`` has elapsed
                     since ``last_escalated_at`` --> stays OPEN, quiet
                     (returns ``""`` — no re-fire on every subsequent
                     occurrence while already escalated).
        OPEN      -- an occurrence AFTER ``window_seconds`` has elapsed since
                     ``last_escalated_at`` --> ages to HALF_OPEN first, then
                     the SAME occurrence immediately re-escalates
                     HALF_OPEN->OPEN (a recurrence during probation — mirrors
                     circuit_breaker's "HALF_OPEN + error -> OPEN") --
                     returns ``STATE_OPEN`` again, bumping ``rung``.

    A CLOSED window that goes quiet for longer than ``window_seconds``
    resets the occurrence count (a fresh escalation cycle) rather than
    accumulating forever — mirrors circuit_breaker's sliding-window intent,
    applied as a fixed-window reset rather than a per-timestamp sliding list
    (this state is one small durable JSON object per identity, not an
    unbounded timestamp list).
    """
    if (
        state.state == STATE_OPEN
        and state.last_escalated_at is not None
        and (now - state.last_escalated_at).total_seconds() >= window_seconds
    ):
        state.state = STATE_HALF_OPEN

    if state.state in (STATE_HALF_OPEN, STATE_OPEN):
        state.occurrence_count += 1
        state.last_occurrence_at = now
        if state.state == STATE_HALF_OPEN:
            state.state = STATE_OPEN
            state.rung += 1
            state.last_escalated_at = now
            return STATE_OPEN
        return ""  # still OPEN, within the cooldown window -- quiet

    # STATE_CLOSED
    if (now - state.first_seen_at).total_seconds() > window_seconds:
        state.first_seen_at = now
        state.occurrence_count = 1
    else:
        state.occurrence_count += 1
    state.last_occurrence_at = now

    if state.occurrence_count >= threshold:
        state.state = STATE_OPEN
        state.rung += 1
        state.last_escalated_at = now
        return STATE_OPEN
    return ""


def record_occurrence(
    identity: str,
    *,
    window_seconds: float,
    threshold: int = DEFAULT_ESCALATION_THRESHOLD,
    now: datetime | None = None,
) -> str | None:
    """Record one occurrence of ``identity`` and return the transition.

    Returns:
        ``"OPEN"`` (:data:`alerting_service.circuit_breaker.STATE_OPEN`) on a
        genuine CLOSED->OPEN or HALF_OPEN->OPEN crossing (dispatch-worthy);
        ``""`` when no transition happened (below threshold, or already OPEN
        and still within its cooldown — mute); ``None`` when the durable
        state itself could not be read/written (fail OPEN — see the module
        docstring's "Return-value contract" section; callers must treat
        ``None`` like ``"OPEN"``, never like ``""``).

    Never raises.
    """
    now = now or datetime.now(UTC)
    bucket = state_bucket()
    if not bucket:
        return None
    try:
        state = _read_state(bucket, identity) or _bootstrap_state(identity, now)
        transition = _advance_state(state, now=now, window_seconds=window_seconds, threshold=threshold)
        _write_state(bucket, state)
        return transition
    except Exception as exc:  # noqa: broad-except — durable ladder must never break the escalation hop
        logger.warning("escalation ladder: record_occurrence failed for %s (failing open): %s", identity, exc)
        return None


def get_state(identity: str) -> LadderState | None:
    """Read-only snapshot of ``identity``'s current durable ladder state, or
    ``None`` when absent/unresolvable/unreadable.

    Mirrors circuit_breaker.get_state()'s read-only-accessor role but
    returns the full :class:`LadderState` (occurrence count / first-seen /
    rung / last-escalated) rather than a bare state name — Phase 4's
    deployment-service reader needs the full record to thread recurrence
    history into a filed issue doc body. Never raises.
    """
    bucket = state_bucket()
    if not bucket:
        return None
    return _read_state(bucket, identity)
