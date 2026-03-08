"""Shared pytest fixtures for the alerting-service test suite."""

from __future__ import annotations

import pytest


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
