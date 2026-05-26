"""Integration test for the alerting-service kill-switch publisher hook.

Per ``alerting_service_live_rules_2026_05_07`` Migrated-issues 2026-05-08:
when an alert with code matching ``KILL_SWITCH_*`` fires through
``route_event``, the router MUST emit a typed :class:`KillSwitchEvent` to the
in-process :class:`KillSwitchBus` after channel dispatch. Subscribers (the live
execution-service halt-pump in production) consume the bus event and halt the
correctly-scoped surface (GLOBAL platform halt vs VENUE per-venue halt).

These tests exercise the FULL routing path with PagerDuty + Telegram HTTP
calls + the GCS persist call mocked, leaving only the in-process kill-switch
bus subscriber under test. Per workspace test policy: no live cloud, no
``--no-block-network``-required calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
from unified_api_contracts import KillSwitchScope
from unified_trading_library import (
    KillSwitchEvent,
    KillSwitchEventType,
    get_kill_switch_bus,
    reset_kill_switch_bus,
)

from alerting_service.notifiers import router as router_module
from alerting_service.notifiers.router import route_event

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_bus_and_dedup() -> Iterator[None]:
    """Each test starts with a fresh KillSwitchBus singleton + cleared dedup window."""
    reset_kill_switch_bus()
    # Clear the module-level deduplicator's state so distinct tests firing the
    # same event_name don't suppress each other.
    router_module._deduplicator._seen.clear()  # test isolation
    # Reset the lru-cached config so each test's patched config takes effect.
    router_module._get_cloud_config.cache_clear()
    yield
    reset_kill_switch_bus()


def _make_http_response(status_code: int, text: str = "ok") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    return resp


def _httpx_post_dispatch(url: str, **_kwargs: object) -> MagicMock:
    """URL-based httpx.post stub — PagerDuty -> 202, Telegram -> 200, anything else -> 200."""
    if "pagerduty.com" in url:
        return _make_http_response(202)
    if "api.telegram.org" in url:
        return _make_http_response(200)
    return _make_http_response(200)


def _make_router_config() -> MagicMock:
    """Mock AlertingSystemConfig with Telegram + PagerDuty creds + LIVE_ALERT_RULES routing."""
    mock_cfg = MagicMock()
    mock_cfg.telegram_bot_token = "test-bot-token"
    mock_cfg.telegram_chat_id = "test-chat-id"
    mock_cfg.gcp_project_id = "test-project"
    # Mirror the legacy default so route_event finds a matching rule + delivers.
    mock_cfg.routing_rules = [
        {
            "event_pattern": "KILL_SWITCH_*",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        {
            "event_pattern": "CIRCUIT_BREAKER_OPEN",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
    ]
    return mock_cfg


def _make_pd_secret_client() -> MagicMock:
    mock_client = MagicMock()
    mock_client.get_secret.return_value = "test-routing-key"
    return mock_client


# ---------------------------------------------------------------------------
# Tests — happy path per scope
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_kill_switch_defi_liquidation_risk_publishes_global_scope_event() -> None:
    """KILL_SWITCH_DEFI_LIQUIDATION_RISK -> KillSwitchEvent(scope=GLOBAL, FIRED)."""
    received: list[KillSwitchEvent] = []
    get_kill_switch_bus().subscribe(received.append)

    pd_cfg = MagicMock(gcp_project_id="test-project")
    pd_secret = _make_pd_secret_client()

    with (
        patch(
            "alerting_service.notifiers.router.AlertingSystemConfig",
            return_value=_make_router_config(),
        ),
        patch(
            "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
            return_value=pd_cfg,
        ),
        patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
        patch("httpx.post", side_effect=_httpx_post_dispatch),
        patch("alerting_service.notifiers.router._persist_delivery_record"),
        patch("alerting_service.notifiers.router._persist_config_snapshot"),
        patch("alerting_service.notifiers.pagerduty.log_event"),
    ):
        route_event(
            "KILL_SWITCH_DEFI_LIQUIDATION_RISK",
            {"message": "Aave HF=1.05 — auto-deleverage trigger"},
        )

    assert len(received) == 1, "publisher hook MUST emit exactly one KillSwitchEvent"
    event = received[0]
    assert event.event_type is KillSwitchEventType.FIRED
    assert event.scope is KillSwitchScope.GLOBAL
    assert event.scope_key is None  # GLOBAL ignores scope_key
    assert event.fired_by == "alerting-service"
    assert "Aave HF" in event.reason


@pytest.mark.integration
def test_kill_switch_venue_disconnect_publishes_venue_scope_event() -> None:
    """KILL_SWITCH_VENUE_DISCONNECT -> KillSwitchEvent(scope=VENUE, scope_key=<venue>)."""
    received: list[KillSwitchEvent] = []
    get_kill_switch_bus().subscribe(received.append)

    pd_cfg = MagicMock(gcp_project_id="test-project")
    pd_secret = _make_pd_secret_client()

    with (
        patch(
            "alerting_service.notifiers.router.AlertingSystemConfig",
            return_value=_make_router_config(),
        ),
        patch(
            "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
            return_value=pd_cfg,
        ),
        patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
        patch("httpx.post", side_effect=_httpx_post_dispatch),
        patch("alerting_service.notifiers.router._persist_delivery_record"),
        patch("alerting_service.notifiers.router._persist_config_snapshot"),
        patch("alerting_service.notifiers.pagerduty.log_event"),
    ):
        route_event(
            "KILL_SWITCH_VENUE_DISCONNECT",
            {
                "message": "Bybit feed silence > 60s",
                "venue": "bybit",
            },
        )

    assert len(received) == 1
    event = received[0]
    assert event.scope is KillSwitchScope.VENUE
    assert event.scope_key == "bybit"
    assert event.event_type is KillSwitchEventType.FIRED


@pytest.mark.integration
def test_kill_switch_portfolio_drawdown_publishes_global_scope_event() -> None:
    """KILL_SWITCH_PORTFOLIO_DRAWDOWN -> KillSwitchEvent(scope=GLOBAL)."""
    received: list[KillSwitchEvent] = []
    get_kill_switch_bus().subscribe(received.append)

    pd_cfg = MagicMock(gcp_project_id="test-project")
    pd_secret = _make_pd_secret_client()

    with (
        patch(
            "alerting_service.notifiers.router.AlertingSystemConfig",
            return_value=_make_router_config(),
        ),
        patch(
            "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
            return_value=pd_cfg,
        ),
        patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
        patch("httpx.post", side_effect=_httpx_post_dispatch),
        patch("alerting_service.notifiers.router._persist_delivery_record"),
        patch("alerting_service.notifiers.router._persist_config_snapshot"),
        patch("alerting_service.notifiers.pagerduty.log_event"),
    ):
        route_event(
            "KILL_SWITCH_PORTFOLIO_DRAWDOWN",
            {"message": "Portfolio drawdown 5.2% > 5% threshold"},
        )

    assert len(received) == 1
    assert received[0].scope is KillSwitchScope.GLOBAL


# ---------------------------------------------------------------------------
# Tests — negative path
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_non_kill_switch_alert_does_not_publish_event() -> None:
    """Non-KILL_SWITCH_* AlertCodes MUST NOT trigger the publisher hook.

    Anchors the validator semantics: a CIRCUIT_BREAKER_OPEN alert pages on-call
    via channel dispatch but does NOT halt downstream subscribers — that's
    reserved for the kill-switch family.
    """
    received: list[KillSwitchEvent] = []
    get_kill_switch_bus().subscribe(received.append)

    pd_cfg = MagicMock(gcp_project_id="test-project")
    pd_secret = _make_pd_secret_client()

    with (
        patch(
            "alerting_service.notifiers.router.AlertingSystemConfig",
            return_value=_make_router_config(),
        ),
        patch(
            "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
            return_value=pd_cfg,
        ),
        patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
        patch("httpx.post", side_effect=_httpx_post_dispatch),
        patch("alerting_service.notifiers.router._persist_delivery_record"),
        patch("alerting_service.notifiers.router._persist_config_snapshot"),
        patch("alerting_service.notifiers.pagerduty.log_event"),
    ):
        route_event(
            "CIRCUIT_BREAKER_OPEN",
            {"message": "Bybit circuit OPEN", "venue": "bybit"},
        )

    assert received == [], "non-KILL_SWITCH alert MUST NOT publish a KillSwitchEvent"


@pytest.mark.integration
def test_publisher_hook_subscriber_failure_does_not_block_channel_dispatch() -> None:
    """A subscriber that raises must NOT abort route_event — KillSwitchBus._deliver
    swallows + logs per its contract, channel dispatch must complete normally."""

    def crashing_subscriber(_event: KillSwitchEvent) -> None:
        raise RuntimeError("subscriber crashed")

    get_kill_switch_bus().subscribe(crashing_subscriber)

    pd_cfg = MagicMock(gcp_project_id="test-project")
    pd_secret = _make_pd_secret_client()

    with (
        patch(
            "alerting_service.notifiers.router.AlertingSystemConfig",
            return_value=_make_router_config(),
        ),
        patch(
            "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
            return_value=pd_cfg,
        ),
        patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
        patch("httpx.post", side_effect=_httpx_post_dispatch),
        patch("alerting_service.notifiers.router._persist_delivery_record"),
        patch("alerting_service.notifiers.router._persist_config_snapshot"),
        patch("alerting_service.notifiers.pagerduty.log_event"),
    ):
        # Should not raise — channel dispatch is the contract; bus is best-effort.
        route_event(
            "KILL_SWITCH_DEFI_LIQUIDATION_RISK",
            {"message": "Aave HF crit"},
        )
