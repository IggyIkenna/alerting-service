"""Shared pytest fixtures for the alerting-service test suite."""

from __future__ import annotations

import pytest
from unified_events_interface import MockEventSink, setup_events


@pytest.fixture(autouse=True, scope="session")
def _init_event_logging() -> None:
    """Initialize event logging once per test session using MockEventSink.

    Tests that call log_event() (e.g. auth failure paths in verify_api_key)
    require setup_events() to have been called first.
    """
    setup_events(service_name="alerting-service", mode="test", sink=MockEventSink())


@pytest.fixture(autouse=True)
def _clear_notifier_config_caches() -> None:
    """Clear lru_cache on _get_cloud_config in both notifiers before every test.

    Both pagerduty._get_cloud_config() and slack._get_cloud_config() are
    decorated with @lru_cache(maxsize=1). Without clearing between tests,
    the first test that calls send_event() or send_message() populates the
    cache and all subsequent tests — even those that patch UnifiedCloudConfig
    — receive the stale cached value instead of their mock.
    """
    from alerting_service.notifiers import pagerduty, slack

    pagerduty._get_cloud_config.cache_clear()
    slack._get_cloud_config.cache_clear()
