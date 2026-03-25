"""Shared pytest fixtures for the alerting-service test suite."""

from __future__ import annotations

import pytest
from unified_trading_library import MockEventSink, setup_events


@pytest.fixture(autouse=True, scope="session")
def _init_event_logging() -> None:
    """Initialize event logging once per test session using MockEventSink.

    Tests that call log_event() (e.g. auth failure paths in verify_api_key)
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
