"""Shared pytest fixtures for the alerting-service test suite."""

from __future__ import annotations

import pytest
from unified_trading_library import MockEventSink, setup_events


@pytest.fixture(autouse=True, scope="session")
def _init_event_logging() -> None:
    """Initialize event logging once per test session using MockEventSink.

    Tests that call log_event() (e.g. auth failure paths in create_api_auth)
    require setup_events() to have been called first.
    """
    setup_events(service_name="alerting-service", mode="test", sink=MockEventSink())


@pytest.fixture(autouse=True)
def _clear_notifier_config_caches() -> None:
    """Clear lru_cache on _get_cloud_config in all notifiers before every test.

    pagerduty, slack, telegram, and router all cache their config.
    Without clearing between tests, the first test that populates the
    cache contaminates all subsequent tests.
    """
    from alerting_service.notifiers import pagerduty, slack
    from alerting_service.notifiers.router import _get_cloud_config as router_get_config
    from alerting_service.persistence.storage_store import _get_cloud_config as storage_get_config

    pagerduty._get_cloud_config.cache_clear()
    slack._get_cloud_config.cache_clear()
    router_get_config.cache_clear()
    storage_get_config.cache_clear()


@pytest.fixture(autouse=True)
def _reset_deduplicator() -> None:
    """Reset the module-level deduplicator before each test.

    Without this, dedup state leaks across tests causing false suppression.
    """
    from alerting_service.notifiers.router import _deduplicator

    _deduplicator._seen.clear()


class _NoOpRecurringCooldownStore:
    """Stand-in for ``AlertStorageStore`` that never touches real/local-mock GCS.

    Used as the DEFAULT for every test (see ``_isolate_recurring_cooldown_storage``
    below) — a test that wants to exercise the persisted layer for real injects its
    own store (``RecurringCooldownState(store=...)`` or a direct
    ``_get_recurring_cooldown_state`` patch), which bypasses this default entirely.
    """

    def read_cooldown_state(self) -> dict[str, object]:
        return {}

    def write_cooldown_state(self, state: dict[str, object]) -> None:
        return None


@pytest.fixture(autouse=True)
def _isolate_recurring_cooldown_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset + fully isolate the GCS-persisted recurring-cooldown layer before each
    test (``core/recurring_dedup_persistence.py``,
    dp_cron_did_not_fire_dedup_state_lost_on_redeploy_2026_08_18.md fix).

    Two failure modes without this: (1) the module-level singleton is process-global
    by design (it must survive across route_event() calls within one real container),
    so one test's ``record()`` leaks into a LATER test reusing the same event/identity;
    (2) even resetting the singleton isn't enough if the test-mode storage client is a
    real local-filesystem-backed mock (it is) — a fresh singleton just reloads the
    PRIOR test's write from disk. Mirrors test_escalation_ladder.py's own
    fake-storage-client isolation pattern, applied globally so no router-level test
    that isn't specifically testing this layer has to know about it.
    """
    from alerting_service.core import recurring_dedup_persistence

    recurring_dedup_persistence.reset_recurring_cooldown_state_for_tests()
    monkeypatch.setattr(recurring_dedup_persistence, "AlertStorageStore", _NoOpRecurringCooldownStore)
