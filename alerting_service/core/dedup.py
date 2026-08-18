"""
Alert deduplication with TTL-based expiry.

Prevents the same alert from being routed multiple times within a
configurable time window.  The dedup key is derived from the event
name and a hash of its **identity** details — the VOLATILE / render-only
fields (the climbing heartbeat age, the formatted message, timestamps,
the umbrella/cloud render context, log links) are EXCLUDED from the hash
so that the same logical alert collapses to one key across sweeps and
across emit-shape variants.

Why exclude volatile fields (2026-06-24 fix): a recurring lifecycle
alert like ``DP_VM_STALL`` was re-firing on every monitor sweep because
its ``details`` carried the changing ``heartbeat_age`` + a ``message``
embedding that age ("…heartbeat 28m stale" → "…33m stale"), so each
sweep hashed to a *different* dedup key and slipped past the window.
Worse, two emit paths (a rich payload with umbrella/cloud + a bare one)
for the SAME vm hashed differently and both delivered.  Hashing only the
stable IDENTITY fields makes the same vm+event collapse to one key, and a
per-call ``ttl_override`` lets the router hold that key for a cooldown
window (≥ the monitor sweep interval) so an ongoing stall pings ONCE per
window — re-alerting only when it resolves and recurs.

GCS-persisted cooldown state for the ``ttl_override`` subset (2026-08-18
fix, ``dp_cron_did_not_fire_dedup_state_lost_on_redeploy_2026_08_18.md``):
``_seen`` is a plain in-process dict, and a Cloud Run service can redeploy
(a fresh container/process) far more often than a recurring cooldown window
— confirmed live: 5 ``dp-alerting-subscriber`` revisions in ~3.25h, one gap
17min shorter than the 1800s ``DP_CRON_DID_NOT_FIRE`` cooldown itself. Every
redeploy silently wiped ``_seen``, so the very next fire for ANY
still-cooling-down identity was treated as first-occurrence and delivered —
structurally defeating every entry in ``router._RECURRING_ALERT_COOLDOWNS``,
independent of which detector/event was involved. An optional
``persisted_store_factory`` closes this: entries recorded with a
non-``None`` ``ttl_override`` (the recurring subset only — the plain 60s
default path is already shorter than any plausible redeploy gap and is
never persisted) round-trip through GCS, loaded once lazily on the first
``is_duplicate`` call and re-written after each new persist-eligible entry.
Fails OPEN on any read/parse/write error, same direction as
``deployment_service.data_pipeline_monitors.renag_tracker.RenagTracker``.
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Protocol, cast

logger = logging.getLogger(__name__)

# Render-only / per-fire-volatile detail keys excluded from the dedup hash.
# These change between otherwise-identical fires (or are present in one emit
# shape and absent in another), so including them defeats deduplication. The
# IDENTITY of an alert (vm_name / venue / instrument / asset_group /
# deployment_id / data_type / reason / error_code …) stays in the key.
_VOLATILE_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        # the climbing staleness / age the watcher recomputes every sweep
        "heartbeat_age",
        "heartbeat_age_min",
        "heartbeat_age_minutes",
        "heartbeat_age_seconds",
        "heartbeat_age_sec",
        "stale",
        "stale_minutes",
        "stale_seconds",
        "stale_for_seconds",
        "age",
        "age_minutes",
        "age_seconds",
        "last_heartbeat",
        # human-formatted text (often embeds the volatile age) + explainers
        "message",
        "summary",
        "detail",
        "explain",
        # wall-clock timestamps — always differ between fires
        "timestamp",
        "ts",
        "time",
        "emitted_at",
        "created_at",
        "observed_at",
        "last_seen",
        # render-only context present in the "rich" emit shape but not the bare
        # one (collapsing the two shapes to one key)
        "umbrella",
        "cloud",
        # log links / traces (per-fire URIs)
        "log_url",
        "log_bucket",
        "run_log",
        "run_log_uri",
        "run_log_tail",
        "trace",
        "trace_id",
        # subscriber-injected per-delivery envelope metadata (alert_subscriber
        # `_unwrap_utl_envelope`/`_process_message`). ``correlation_id`` is a
        # fresh uuid (or UTL metadata value) on EVERY message — keeping it in
        # the key made every delivery hash uniquely, so the recurring-event
        # cooldowns (DP_VM_PREEMPTED / DP_VM_GONE_NO_CAPTURE / ...) never
        # engaged and the same logical alert re-paged every sweep
        # (2026-08-10 storm). ``service_name`` / ``source`` are stable but
        # render/envelope metadata, not logical identity — same treatment.
        "correlation_id",
        "service_name",
        "source",
        # DP-LIVE-004 (live_stream_watcher.check_live_capture_productivity) recomputes this
        # every ~15min sweep and embeds it in the CRITICAL DP_CRON_DID_NOT_FIRE summary — an
        # exact-match miss (only "age"/"age_hours" etc. were listed, not this producer's own
        # key name) let it slip into the identity hash, defeating the 1800s cooldown every
        # single sweep (2026-08-17 finding: 69 DP_CRON_DID_NOT_FIRE messages/24h on 2 live VMs).
        "attempted_age_hours",
    }
)

# Suffix patterns for volatile per-sweep age/staleness fields not yet given an exact-match
# entry above — belt-and-braces so a NEW detector's own age-field name doesn't reproduce the
# same class of dedup-defeat bug (2026-08-17).
_VOLATILE_DETAIL_KEY_SUFFIXES: tuple[str, ...] = (
    "_age_hours",
    "_age_days",
    "_age_minutes",
    "_age_seconds",
)


class CooldownPersistedStore(Protocol):
    """Structural type for a GCS-backed cooldown-state store.

    Matches ``AlertStorageStore.read_cooldown_state`` /
    ``AlertStorageStore.write_cooldown_state``
    (``alerting_service/persistence/storage_store.py``) without importing
    that module here — keeps ``core.dedup`` decoupled from the GCS
    persistence layer + its cloud-config machinery, and lets unit tests
    construct an ``AlertDeduplicator`` with a bare fake instead of a real
    GCS-backed client.
    """

    def read_cooldown_state(self) -> dict[str, object]: ...

    def write_cooldown_state(self, state: dict[str, object]) -> None: ...


class AlertDeduplicator:
    """TTL-based alert deduplicator.

    Each unique ``(event_name, hash_of_identity_details)`` pair is
    remembered for its TTL.  Calling ``is_duplicate`` within that window
    returns ``True`` and suppresses the alert; after the TTL expires the
    same alert is treated as new.

    Args:
        ttl_seconds: Default window (seconds) a seen alert is considered a
            duplicate.  Defaults to 60.  A per-call ``ttl_override`` (e.g. a
            longer cooldown for recurring lifecycle WARN alerts) wins for the
            entry it records.
        persisted_store_factory: Optional zero-arg callable returning a
            :class:`CooldownPersistedStore`. When set, entries recorded with
            a non-``None`` ``ttl_override`` survive a fresh process/container
            by round-tripping through GCS (see module docstring). Resolved
            LAZILY — on the first ``is_duplicate`` call, not at ``__init__``
            time — so constructing a real store (which touches cloud config)
            never happens at import time, only on first actual use.
    """

    def __init__(
        self,
        ttl_seconds: float = 60.0,
        persisted_store_factory: Callable[[], CooldownPersistedStore] | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        # Maps dedup key -> (timestamp of first occurrence, effective ttl).
        self._seen: dict[str, tuple[float, float]] = {}
        self._persisted_store_factory = persisted_store_factory
        self._persisted_store: CooldownPersistedStore | None = None
        self._persisted_loaded = False
        # Wall-clock mirror of the persist-eligible (ttl_override is not
        # None) subset of `_seen` -- key -> (epoch_seconds, ttl). `_seen`
        # itself is keyed by `time.monotonic()`, which is meaningless across
        # a process restart, so this is what actually round-trips via GCS.
        self._persisted_entries: dict[str, tuple[float, float]] = {}

    @staticmethod
    def _make_key(event_name: str, details: dict[str, object]) -> str:
        """Build a dedup key from event name + a stable hash of IDENTITY details.

        Volatile / render-only fields (``_VOLATILE_DETAIL_KEYS``) are dropped
        before hashing so the same logical alert hashes identically across
        sweeps and emit-shape variants.
        """
        identity = {
            k: v
            for k, v in details.items()
            if k not in _VOLATILE_DETAIL_KEYS and not k.endswith(_VOLATILE_DETAIL_KEY_SUFFIXES)
        }
        details_json = json.dumps(identity, sort_keys=True, default=str)
        details_hash = hashlib.sha256(details_json.encode("utf-8")).hexdigest()[:16]
        return f"{event_name}:{details_hash}"

    def _evict_expired(self, now: float) -> None:
        """Remove entries older than their own (possibly overridden) TTL."""
        expired = [key for key, (ts, ttl) in self._seen.items() if (now - ts) > ttl]
        for key in expired:
            del self._seen[key]

    def _ensure_persisted_loaded(self) -> None:
        """Lazily resolve + load persisted cooldown state, once per instance.

        Runs on the FIRST ``is_duplicate`` call only (a cheap flag check on
        every call thereafter) — this is what lets a FRESH instance (a new
        Cloud Run container after a redeploy) pick back up a cooldown a PRIOR
        instance persisted, closing the gap this exists to fix. Fails OPEN:
        any read/parse error just means this run behaves like persistence was
        never configured (today's status quo), never a hard failure.
        """
        if self._persisted_loaded or self._persisted_store_factory is None:
            return
        self._persisted_loaded = True  # attempt at most once, success or fail
        try:
            store = self._persisted_store_factory()
            raw = store.read_cooldown_state()
        except Exception as exc:
            logger.warning("AlertDeduplicator: failed to load persisted cooldown state: %s", exc)
            return

        now_wall = time.time()
        now_mono = time.monotonic()
        for key, raw_entry in raw.items():
            if not isinstance(raw_entry, dict):
                continue
            entry = cast("dict[str, object]", raw_entry)
            ts = entry.get("ts")
            ttl = entry.get("ttl")
            if not isinstance(ts, (int, float)) or not isinstance(ttl, (int, float)):
                continue
            age = now_wall - float(ts)
            if age < 0 or age >= float(ttl):
                continue  # already expired (or clock skew) -- fail open, don't suppress
            self._seen[key] = (now_mono - age, float(ttl))
            self._persisted_entries[key] = (float(ts), float(ttl))
        self._persisted_store = store

    def _flush_persisted(self) -> None:
        """Best-effort write-through of the persist-eligible subset to GCS.

        Never raises -- a failed write just means the NEXT redeploy inside
        this entry's cooldown window loses it (today's status quo), not that
        THIS process's own in-memory dedup breaks.
        """
        if self._persisted_store is None:
            return
        now_wall = time.time()
        self._persisted_entries = {
            k: (ts, ttl) for k, (ts, ttl) in self._persisted_entries.items() if (now_wall - ts) < ttl
        }
        state: dict[str, object] = {k: {"ts": ts, "ttl": ttl} for k, (ts, ttl) in self._persisted_entries.items()}
        try:
            self._persisted_store.write_cooldown_state(state)
        except Exception as exc:
            logger.warning("AlertDeduplicator: failed to persist cooldown state: %s", exc)

    def is_duplicate(
        self,
        event_name: str,
        details: dict[str, object],
        ttl_override: float | None = None,
    ) -> bool:
        """Check whether an alert is a duplicate within the TTL window.

        If the alert has not been seen (or its TTL expired), records it
        and returns ``False``.  If it was already seen within the TTL,
        returns ``True``.

        Args:
            event_name: Canonical event name (e.g. ``"KILL_SWITCH_ACTIVATED"``).
            details: Event details dictionary (identity fields are hashed;
                volatile/render fields are ignored — see ``_make_key``).
            ttl_override: Optional per-call window (seconds). When set, the
                recorded entry is held for this long instead of the default
                ``ttl_seconds`` — used to give recurring lifecycle WARN alerts
                a cooldown ≥ the monitor sweep interval so an ongoing condition
                pings once per window, not every sweep. Also the signal for
                GCS persistence (see ``persisted_store_factory``): a ``None``
                override (the plain 60s-default path) is never persisted.

        Returns:
            ``True`` if the alert is a duplicate, ``False`` otherwise.
        """
        self._ensure_persisted_loaded()
        now = time.monotonic()
        self._evict_expired(now)

        key = self._make_key(event_name, details)
        if key in self._seen:
            return True

        effective_ttl = ttl_override if ttl_override is not None else self._ttl
        self._seen[key] = (now, effective_ttl)

        if ttl_override is not None:
            self._persisted_entries[key] = (time.time(), effective_ttl)
            self._flush_persisted()

        return False
