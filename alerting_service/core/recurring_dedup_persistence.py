"""GCS-persisted last-seen timestamps for ``_RECURRING_ALERT_COOLDOWNS``-eligible
alert identities.

Closes ``dp_cron_did_not_fire_dedup_state_lost_on_redeploy_2026_08_18.md``:
``AlertDeduplicator._seen`` is a plain in-process dict, wiped on every fresh Cloud Run
revision — and ``dp-alerting-subscriber`` redeploys far more often (5 revisions/3.25h
measured, one gap only 17min) than any recurring-cooldown window (>=1800s), so a
redeploy landing inside an open cooldown silently resets it and the very next fire for
EVERY currently-cooling-down identity is delivered as if it were a first occurrence —
defeating every ``_RECURRING_ALERT_COOLDOWNS`` entry fleet-wide.

Reuses ``AlertStorageStore.read_cooldown_state()``/``write_cooldown_state()``
(``alerting/state/cooldowns.json``) rather than inventing a new GCS blob or bucket —
that persistence layer already shipped but had ZERO production caller until this fix,
the same "built but not called" anti-pattern already documented for the revocation
actuator in ``codex/05-infrastructure/data-pipeline-alerts.md``.

Scope is deliberately narrow (per the issue doc's own risk note): only events carrying
a ``_RECURRING_ALERT_COOLDOWNS`` ``ttl_override`` touch this layer — the general
60s-default dedup path (every OTHER event in the system) stays in-process-only,
unchanged, so this does not add GCS I/O to alerting-service's hot path as a whole.
State is loaded once per process/container lifetime (cached in memory) and written
through only on an actual new delivery (cooldown-gated, so writes are infrequent) —
never a GCS read on every alert.
"""

from __future__ import annotations

import logging
import time

from alerting_service.persistence.storage_store import AlertStorageStore

from .dedup import compute_dedup_key

logger = logging.getLogger(__name__)


class RecurringCooldownState:
    """Process-lifetime cache of the durable recurring-cooldown timestamps.

    ``should_suppress``/``record`` both fail OPEN (never suppress, never raise) on any
    GCS read/write error — a durable-state outage must never be the reason a CRITICAL
    data-pipeline finding stops paging (mirrors ``RenagTracker``'s and
    ``escalation_ladder.py``'s documented fail-open direction).
    """

    def __init__(self, store: AlertStorageStore | None = None) -> None:
        self._store = store
        self._loaded = False
        self._last_emitted_at: dict[str, float] = {}

    def _get_store(self) -> AlertStorageStore:
        if self._store is None:
            self._store = AlertStorageStore()
        return self._store

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self._get_store().read_cooldown_state()
            self._last_emitted_at = {str(k): float(v) for k, v in raw.items() if isinstance(v, int | float)}
        except Exception as exc:  # noqa: broad-except -- fail open, never suppress on a load error
            logger.warning("RecurringCooldownState: load failed, failing open: %s", exc)
            self._last_emitted_at = {}

    def should_suppress(self, event_name: str, details: dict[str, object], ttl_seconds: float) -> bool:
        """``True`` only when a durably-recorded prior emission for this identity is
        still within ``ttl_seconds``. Read-only — never mutates state."""
        self._ensure_loaded()
        last = self._last_emitted_at.get(compute_dedup_key(event_name, details))
        if last is None:
            return False
        return (time.time() - last) < ttl_seconds

    def record(self, event_name: str, details: dict[str, object]) -> None:
        """Record this identity's emission NOW and persist. Call only immediately after
        the caller has decided to actually deliver the alert (never on a
        suppressed/duplicate call, or the cooldown clock advances with nothing sent)."""
        self._ensure_loaded()
        self._last_emitted_at[compute_dedup_key(event_name, details)] = time.time()
        try:
            self._get_store().write_cooldown_state(dict(self._last_emitted_at))
        except Exception as exc:  # noqa: broad-except -- persistence is best-effort
            logger.warning("RecurringCooldownState: persist failed: %s", exc)


_singleton: RecurringCooldownState | None = None


def get_recurring_cooldown_state() -> RecurringCooldownState:
    """Process-wide singleton, lazily constructed (avoids GCS/config access at module
    import time — mirrors ``router._get_storage_store()``'s own lazy-singleton
    pattern)."""
    global _singleton
    if _singleton is None:
        _singleton = RecurringCooldownState()
    return _singleton


def reset_recurring_cooldown_state_for_tests() -> None:
    """Test-only reset of the module-level singleton (mirrors
    ``router._reset_coalesce_window_for_tests``)."""
    global _singleton
    _singleton = None
