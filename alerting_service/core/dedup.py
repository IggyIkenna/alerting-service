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
"""

import hashlib
import json
import time

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


def _compute_identity_hash_key(event_name: str, details: dict[str, object]) -> str:
    """Build a dedup key from event name + a stable hash of IDENTITY details.

    Volatile / render-only fields (``_VOLATILE_DETAIL_KEYS``) are dropped before
    hashing so the same logical alert hashes identically across sweeps and emit-shape
    variants. Module-private top-level function (not a class member) so BOTH
    :meth:`AlertDeduplicator._make_key` and the public :func:`compute_dedup_key` below
    share ONE implementation without either reaching into the other across a
    class/module boundary (avoids basedpyright ``reportPrivateUsage``).
    """
    identity = {
        k: v
        for k, v in details.items()
        if k not in _VOLATILE_DETAIL_KEYS and not k.endswith(_VOLATILE_DETAIL_KEY_SUFFIXES)
    }
    details_json = json.dumps(identity, sort_keys=True, default=str)
    details_hash = hashlib.sha256(details_json.encode("utf-8")).hexdigest()[:16]
    return f"{event_name}:{details_hash}"


def compute_dedup_key(event_name: str, details: dict[str, object]) -> str:
    """Public entry point for the identity-hash key derivation, so an EXTERNAL durable
    (GCS-persisted) cooldown layer can compute an IDENTICAL key without a
    cross-module private-attribute reach-in — mirrors the "reuse the convention, don't
    re-derive it" call already made in ``alerting_service/escalation_ladder.py``'s
    module docstring."""
    return _compute_identity_hash_key(event_name, details)


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
    """

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        # Maps dedup key -> (timestamp of first occurrence, effective ttl).
        self._seen: dict[str, tuple[float, float]] = {}

    @staticmethod
    def _make_key(event_name: str, details: dict[str, object]) -> str:
        """Build a dedup key from event name + a stable hash of IDENTITY details.

        Volatile / render-only fields (``_VOLATILE_DETAIL_KEYS``) are dropped
        before hashing so the same logical alert hashes identically across
        sweeps and emit-shape variants. Delegates to the module-level
        ``_compute_identity_hash_key`` — see that function's docstring.
        """
        return _compute_identity_hash_key(event_name, details)

    def _evict_expired(self, now: float) -> None:
        """Remove entries older than their own (possibly overridden) TTL."""
        expired = [key for key, (ts, ttl) in self._seen.items() if (now - ts) > ttl]
        for key in expired:
            del self._seen[key]

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
                pings once per window, not every sweep.

        Returns:
            ``True`` if the alert is a duplicate, ``False`` otherwise.
        """
        now = time.monotonic()
        self._evict_expired(now)

        key = self._make_key(event_name, details)
        if key in self._seen:
            return True

        self._seen[key] = (now, ttl_override if ttl_override is not None else self._ttl)
        return False
