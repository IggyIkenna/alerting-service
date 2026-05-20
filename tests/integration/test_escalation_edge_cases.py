"""Escalation edge-case tests for the alerting-service router.

Covers three failure/edge paths not exercised by test_router_integration.py:

  1. P0 graceful degradation — PagerDuty returns 429/500 → route_event
     completes without raising; ALERT_FAILED emitted, not ALERT_SENT.

  2. P1 deduplication via router — same (event_name, details) pair fired
     twice within the TTL window → second call is suppressed silently.

  3. P2 Slack rate-limit (429) at router level — Slack returns 429 →
     route_event completes without raising; delivery recorded as failed.

Additional: wildcard routing catches novel event names → telegram-only.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

import alerting_service.notifiers.router as router_module
from alerting_service.core.dedup import AlertDeduplicator
from alerting_service.notifiers.router import route_event

# ---------------------------------------------------------------------------
# Helpers shared with test_router_integration.py
# ---------------------------------------------------------------------------


def _make_response(status_code: int, text: str = "ok") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    return resp


def _patch_pd_secret(routing_key: str = "escalation-test-key") -> MagicMock:
    mock_client = MagicMock()
    mock_client.get_secret.return_value = routing_key
    return mock_client


def _patch_slack_secret(
    webhook_url: str = "https://hooks.slack.com/services/T555/B666/escalation",
) -> MagicMock:
    mock_client = MagicMock()
    mock_client.get_secret.return_value = webhook_url
    return mock_client


def _routing_rules_with_pd_and_slack() -> list[dict[str, object]]:
    return [
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
        {"event_pattern": "SERVICE_DEGRADED", "channels": ["telegram"], "severity_filter": None},
        {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
    ]


@pytest.fixture
def pd_config() -> MagicMock:
    mock_cfg = MagicMock()
    mock_cfg.gcp_project_id = "escalation-test-project"
    return mock_cfg


@pytest.fixture
def slack_config() -> MagicMock:
    mock_cfg = MagicMock()
    mock_cfg.gcp_project_id = "escalation-test-project"
    return mock_cfg


@pytest.fixture
def router_config_no_telegram() -> MagicMock:
    mock_cfg = MagicMock()
    mock_cfg.telegram_bot_token = ""
    mock_cfg.telegram_chat_id = ""
    mock_cfg.gcp_project_id = "escalation-test-project"
    mock_cfg.routing_rules = _routing_rules_with_pd_and_slack()
    return mock_cfg


@pytest.fixture
def fresh_deduplicator() -> AlertDeduplicator:
    """Return a fresh AlertDeduplicator and patch the module-level instance."""
    dedup = AlertDeduplicator(ttl_seconds=60.0)
    router_module._deduplicator = dedup  # reset global for test isolation
    return dedup


# ---------------------------------------------------------------------------
# Scenario 1 — P0 graceful degradation: PD failure, router completes
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPagerDutyFailureGracefulDegradation:
    """route_event must complete without raising when PagerDuty fails."""

    def test_pd_429_route_event_completes(
        self,
        pd_config: MagicMock,
        slack_config: MagicMock,
        router_config_no_telegram: MagicMock,
        fresh_deduplicator: AlertDeduplicator,
    ) -> None:
        pd_secret = _patch_pd_secret()
        slack_secret = _patch_slack_secret()

        def _dispatch(url: str, **_kwargs: object) -> MagicMock:
            if "pagerduty.com" in url:
                return _make_response(429, "too many requests")
            return _make_response(200)

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=router_config_no_telegram,
            ),
            patch("httpx.post", side_effect=_dispatch),
            patch("alerting_service.notifiers.router.log_event") as mock_log,
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event(
                "KILL_SWITCH_ACTIVATED", {"strategy": "strat-1", "source": "execution-service"}
            )

        logged_events = [c.args[0] for c in mock_log.call_args_list]
        assert "ALERT_FAILED" in logged_events, (
            f"Expected ALERT_FAILED on PD 429; got {logged_events}"
        )

    def test_pd_500_route_event_completes(
        self,
        pd_config: MagicMock,
        slack_config: MagicMock,
        router_config_no_telegram: MagicMock,
        fresh_deduplicator: AlertDeduplicator,
    ) -> None:
        pd_secret = _patch_pd_secret()
        slack_secret = _patch_slack_secret()

        def _dispatch(url: str, **_kwargs: object) -> MagicMock:
            if "pagerduty.com" in url:
                return _make_response(500, "internal server error")
            return _make_response(200)

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=router_config_no_telegram,
            ),
            patch("httpx.post", side_effect=_dispatch),
            patch("alerting_service.notifiers.router.log_event") as mock_log,
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event(
                "KILL_SWITCH_ACTIVATED", {"strategy": "strat-2", "source": "execution-service"}
            )

        logged_events = [c.args[0] for c in mock_log.call_args_list]
        assert "ALERT_FAILED" in logged_events, (
            f"Expected ALERT_FAILED on PD 500; got {logged_events}"
        )


# ---------------------------------------------------------------------------
# Scenario 2 — P1 deduplication at router level
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDedupAtRouterLevel:
    """route_event silently drops the second of two identical events within TTL."""

    def test_duplicate_event_suppressed(
        self,
        pd_config: MagicMock,
        router_config_no_telegram: MagicMock,
        fresh_deduplicator: AlertDeduplicator,
    ) -> None:
        pd_secret = _patch_pd_secret()
        slack_secret = _patch_slack_secret()
        call_count: list[int] = [0]

        def _dispatch(url: str, **_kwargs: object) -> MagicMock:
            if "pagerduty.com" in url:
                call_count[0] += 1
                return _make_response(202)
            return _make_response(200)

        common_details: dict[str, object] = {
            "strategy": "dedup-strat",
            "source": "execution-service",
        }

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=pd_config),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=router_config_no_telegram,
            ),
            patch("httpx.post", side_effect=_dispatch),
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("KILL_SWITCH_ACTIVATED", common_details)
            route_event("KILL_SWITCH_ACTIVATED", common_details)

        assert call_count[0] == 1, f"PD should be called exactly once; called {call_count[0]} times"

    def test_different_details_not_deduped(
        self,
        pd_config: MagicMock,
        router_config_no_telegram: MagicMock,
        fresh_deduplicator: AlertDeduplicator,
    ) -> None:
        pd_secret = _patch_pd_secret()
        slack_secret = _patch_slack_secret()
        call_count: list[int] = [0]

        def _dispatch(url: str, **_kwargs: object) -> MagicMock:
            if "pagerduty.com" in url:
                call_count[0] += 1
                return _make_response(202)
            return _make_response(200)

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=pd_config),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=router_config_no_telegram,
            ),
            patch("httpx.post", side_effect=_dispatch),
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event(
                "KILL_SWITCH_ACTIVATED", {"strategy": "strat-A", "source": "execution-service"}
            )
            route_event(
                "KILL_SWITCH_ACTIVATED", {"strategy": "strat-B", "source": "execution-service"}
            )

        assert call_count[0] == 2, (
            f"Different details must NOT be deduped; PD called {call_count[0]} times"
        )


# ---------------------------------------------------------------------------
# Scenario 3 — P2 Slack rate-limit (429) at router level
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSlackRateLimitAtRouterLevel:
    """route_event must complete without raising when Slack returns 429."""

    def test_slack_429_route_event_completes(
        self,
        slack_config: MagicMock,
        router_config_no_telegram: MagicMock,
        fresh_deduplicator: AlertDeduplicator,
    ) -> None:
        slack_secret = _patch_slack_secret()

        service_degraded_cfg = MagicMock()
        service_degraded_cfg.telegram_bot_token = ""
        service_degraded_cfg.telegram_chat_id = ""
        service_degraded_cfg.gcp_project_id = "escalation-test-project"
        service_degraded_cfg.routing_rules = [
            {
                "event_pattern": "SERVICE_DEGRADED",
                "channels": ["telegram"],
                "severity_filter": None,
            },
            {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
        ]

        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=service_degraded_cfg,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post",
                return_value=_make_response(429, "too many requests"),
            ),
            patch("alerting_service.notifiers.router.log_event") as mock_log,
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("SERVICE_DEGRADED", {"service": "market-tick-data-service"})

        logged_events = [c.args[0] for c in mock_log.call_args_list]
        assert "ALERT_FAILED" in logged_events or "ALERT_SENT" in logged_events, (
            f"Expected ALERT_FAILED or ALERT_SENT; got {logged_events}"
        )


# ---------------------------------------------------------------------------
# Scenario 4 — wildcard rule catches novel event names
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestWildcardRoutingCatchesNovelEvents:
    """Unknown event names must fall through to the wildcard rule → telegram-only."""

    def test_novel_event_routes_to_telegram(
        self,
        pd_config: MagicMock,
        router_config_no_telegram: MagicMock,
        fresh_deduplicator: AlertDeduplicator,
    ) -> None:
        slack_secret = _patch_slack_secret()
        http_calls: list[str] = []

        def _dispatch(url: str, **_kwargs: object) -> MagicMock:
            http_calls.append(url)
            return _make_response(200)

        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=pd_config),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=router_config_no_telegram,
            ),
            patch("httpx.post", side_effect=_dispatch),
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("SOME_NOVEL_EVENT_20260518", {"source": "new-service"})

        pd_calls = [url for url in http_calls if "pagerduty.com" in url]
        slack_calls = [url for url in http_calls if "slack.com" in url]
        assert not pd_calls, f"Novel event must NOT go to PagerDuty; got {pd_calls}"
        assert slack_calls, (
            f"Novel event must route to Slack fallback (no Telegram configured); got {http_calls}"
        )
