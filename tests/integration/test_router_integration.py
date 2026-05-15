"""Integration tests for the alert router.

These tests exercise the full routing logic end-to-end, from route_event()
through to each notifier's send call, with only the outbound HTTP and
Secret Manager calls mocked so no real network I/O occurs.

Routing rules (current):
- Critical events -> PagerDuty + Telegram (or Slack fallback if Telegram not configured)
- All other events -> Telegram (or Slack fallback if Telegram not configured)
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alerting_service.notifiers.router import route_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_http_response(status_code: int, text: str = "ok") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    return resp


def _httpx_post_dispatch(url: str, **kwargs: object) -> MagicMock:
    """URL-based httpx.post dispatcher — PagerDuty -> 202, Telegram -> 200, Slack -> 200."""
    if "pagerduty.com" in url:
        return _make_http_response(202)
    if "api.telegram.org" in url:
        return _make_http_response(200)
    return _make_http_response(200)


def _patch_pd_secret(routing_key: str = "integration-routing-key") -> MagicMock:
    """Return a mock secret client pre-configured with a PagerDuty routing key."""
    mock_client = MagicMock()
    mock_client.get_secret.return_value = routing_key
    return mock_client


def _patch_slack_secret(
    webhook_url: str = "https://hooks.slack.com/services/T111/B222/integration",
) -> MagicMock:
    """Return a mock secret client pre-configured with a Slack webhook URL."""
    mock_client = MagicMock()
    mock_client.get_secret.return_value = webhook_url
    return mock_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pd_config() -> MagicMock:
    mock_cfg = MagicMock()
    mock_cfg.gcp_project_id = "integration-test-project"
    return mock_cfg


@pytest.fixture
def mock_slack_config() -> MagicMock:
    mock_cfg = MagicMock()
    mock_cfg.gcp_project_id = "integration-test-project"
    return mock_cfg


def _default_routing_rules() -> list[dict[str, object]]:
    """Default routing rules matching the legacy hardcoded routing behavior."""
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
        {"event_pattern": "PREFLIGHT_FAILED", "channels": ["telegram"], "severity_filter": None},
        {"event_pattern": "SERVICE_DEGRADED", "channels": ["telegram"], "severity_filter": None},
        {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
    ]


@pytest.fixture
def mock_router_config_no_telegram() -> MagicMock:
    """Router config without Telegram credentials — triggers Slack fallback."""
    mock_cfg = MagicMock()
    mock_cfg.telegram_bot_token = ""
    mock_cfg.telegram_chat_id = ""
    mock_cfg.gcp_project_id = "integration-test-project"
    mock_cfg.routing_rules = _default_routing_rules()
    return mock_cfg


@pytest.fixture
def mock_router_config_with_telegram() -> MagicMock:
    """Router config with Telegram credentials."""
    mock_cfg = MagicMock()
    mock_cfg.telegram_bot_token = "integration-bot-token"
    mock_cfg.telegram_chat_id = "integration-chat-id"
    mock_cfg.gcp_project_id = "integration-test-project"
    mock_cfg.routing_rules = _default_routing_rules()
    return mock_cfg


# ---------------------------------------------------------------------------
# Integration tests: KILL_SWITCH_ACTIVATED -> PagerDuty + Slack fallback
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestKillSwitchRouting:
    """KILL_SWITCH_ACTIVATED must trigger PagerDuty and Slack (when Telegram not configured)."""

    def test_kill_switch_triggers_pagerduty(
        self,
        mock_pd_config: MagicMock,
        mock_slack_config: MagicMock,
        mock_router_config_no_telegram: MagicMock,
    ) -> None:
        pd_secret = _patch_pd_secret()
        slack_secret = _patch_slack_secret()

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch(
                "alerting_service.notifiers.slack.UnifiedCloudConfig",
                return_value=mock_slack_config,
            ),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_no_telegram,
            ),
            patch("httpx.post", side_effect=_httpx_post_dispatch) as mock_http_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("KILL_SWITCH_ACTIVATED", {"strategy": "strat-1", "source": "execution-service"})

        pd_calls = [c for c in mock_http_post.call_args_list if "pagerduty.com" in c.args[0]]
        assert pd_calls, "PagerDuty httpx.post was not called"
        assert pd_calls[0].args[0] == "https://events.pagerduty.com/v2/enqueue"
        pd_json: dict[str, object] = pd_calls[0].kwargs["json"]
        inner = pd_json["payload"]
        assert isinstance(inner, dict)
        assert inner["severity"] == "critical"
        assert "KILL_SWITCH_ACTIVATED" in str(inner["summary"])

    def test_kill_switch_triggers_slack_fallback(
        self,
        mock_pd_config: MagicMock,
        mock_slack_config: MagicMock,
        mock_router_config_no_telegram: MagicMock,
    ) -> None:
        pd_secret = _patch_pd_secret()
        slack_secret = _patch_slack_secret()

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch(
                "alerting_service.notifiers.slack.UnifiedCloudConfig",
                return_value=mock_slack_config,
            ),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_no_telegram,
            ),
            patch("httpx.post", side_effect=_httpx_post_dispatch) as mock_http_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("KILL_SWITCH_ACTIVATED", {"strategy": "strat-1", "source": "execution-service"})

        slack_calls = [c for c in mock_http_post.call_args_list if "slack.com" in c.args[0]]
        assert slack_calls, "Slack httpx.post was not called (fallback)"
        slack_json: dict[str, object] = slack_calls[0].kwargs["json"]
        assert "KILL_SWITCH_ACTIVATED" in str(slack_json["text"])

    def test_kill_switch_triggers_telegram_when_configured(
        self,
        mock_pd_config: MagicMock,
        mock_router_config_with_telegram: MagicMock,
    ) -> None:
        pd_secret = _patch_pd_secret()

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_with_telegram,
            ),
            patch("httpx.post", side_effect=_httpx_post_dispatch) as mock_http_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.telegram.log_event"),
        ):
            route_event("KILL_SWITCH_ACTIVATED", {"strategy": "strat-1", "source": "execution-service"})

        pd_calls = [c for c in mock_http_post.call_args_list if "pagerduty.com" in c.args[0]]
        tg_calls = [c for c in mock_http_post.call_args_list if "api.telegram.org" in c.args[0]]
        slack_calls = [c for c in mock_http_post.call_args_list if "slack.com" in c.args[0]]
        assert pd_calls, "PagerDuty httpx.post was not called"
        assert tg_calls, "Telegram httpx.post was not called"
        assert not slack_calls, "Slack should not be called when Telegram is configured"

    def test_kill_switch_secrets_fetched_from_secret_manager(
        self,
        mock_pd_config: MagicMock,
        mock_slack_config: MagicMock,
        mock_router_config_no_telegram: MagicMock,
    ) -> None:
        """Secret Manager must be consulted for PD routing key and Slack URL (fallback)."""
        pd_secret = _patch_pd_secret()
        slack_secret = _patch_slack_secret()

        with (
            patch(
                "alerting_service.notifiers.pagerduty._get_cloud_config",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret) as mock_pd_sm,
            patch(
                "alerting_service.notifiers.slack._get_cloud_config",
                return_value=mock_slack_config,
            ),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret) as mock_slack_sm,
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_no_telegram,
            ),
            patch("httpx.post", side_effect=_httpx_post_dispatch),
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("KILL_SWITCH_ACTIVATED", {"source": "execution-service"})

        mock_pd_sm.assert_called_once_with(project_id="integration-test-project")
        pd_secret.get_secret.assert_called_once_with("alerting-pagerduty-routing-key")

        mock_slack_sm.assert_called_once_with(project_id="integration-test-project")
        slack_secret.get_secret.assert_called_once_with("alerting-slack-webhook-url")

    def test_kill_switch_routing_key_injected_into_pd_payload(
        self,
        mock_pd_config: MagicMock,
        mock_slack_config: MagicMock,
        mock_router_config_no_telegram: MagicMock,
    ) -> None:
        routing_key = "specific-routing-key-for-integration"
        pd_secret = _patch_pd_secret(routing_key=routing_key)
        slack_secret = _patch_slack_secret()

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch(
                "alerting_service.notifiers.slack.UnifiedCloudConfig",
                return_value=mock_slack_config,
            ),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_no_telegram,
            ),
            patch("httpx.post", side_effect=_httpx_post_dispatch) as mock_http_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("KILL_SWITCH_ACTIVATED", {"source": "execution-service"})

        pd_calls = [c for c in mock_http_post.call_args_list if "pagerduty.com" in c.args[0]]
        assert pd_calls, "PagerDuty httpx.post was not called"
        pd_json: dict[str, object] = pd_calls[0].kwargs["json"]
        assert pd_json["routing_key"] == routing_key

    def test_kill_switch_details_forwarded_to_pagerduty(
        self,
        mock_pd_config: MagicMock,
        mock_slack_config: MagicMock,
        mock_router_config_no_telegram: MagicMock,
    ) -> None:
        pd_secret = _patch_pd_secret()
        slack_secret = _patch_slack_secret()
        event_details: dict[str, object] = {
            "strategy": "momentum-v2",
            "venue": "bybit",
            "source": "execution-service",
        }

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch(
                "alerting_service.notifiers.slack.UnifiedCloudConfig",
                return_value=mock_slack_config,
            ),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_no_telegram,
            ),
            patch("httpx.post", side_effect=_httpx_post_dispatch) as mock_http_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("KILL_SWITCH_ACTIVATED", event_details)

        pd_calls = [c for c in mock_http_post.call_args_list if "pagerduty.com" in c.args[0]]
        assert pd_calls, "PagerDuty httpx.post was not called"
        pd_json: dict[str, object] = pd_calls[0].kwargs["json"]
        inner = pd_json["payload"]
        assert isinstance(inner, dict)
        assert inner["custom_details"] == event_details


# ---------------------------------------------------------------------------
# Integration tests: CIRCUIT_BREAKER_OPEN -> PagerDuty + Telegram/Slack
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCircuitBreakerRouting:
    """CIRCUIT_BREAKER_OPEN must trigger PagerDuty + Telegram (or Slack fallback)."""

    def test_circuit_breaker_triggers_pagerduty_and_telegram(
        self,
        mock_pd_config: MagicMock,
        mock_router_config_with_telegram: MagicMock,
    ) -> None:
        pd_secret = _patch_pd_secret()

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_with_telegram,
            ),
            patch("httpx.post", side_effect=_httpx_post_dispatch) as mock_http_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.telegram.log_event"),
        ):
            route_event("CIRCUIT_BREAKER_OPEN", {"venue": "binance", "source": "execution-service"})

        pd_calls = [c for c in mock_http_post.call_args_list if "pagerduty.com" in c.args[0]]
        tg_calls = [c for c in mock_http_post.call_args_list if "api.telegram.org" in c.args[0]]
        assert len(pd_calls) == 1, "Expected exactly 1 PagerDuty call"
        assert len(tg_calls) == 1, "Expected exactly 1 Telegram call"

    def test_circuit_breaker_pd_payload_severity_critical(
        self,
        mock_pd_config: MagicMock,
        mock_router_config_with_telegram: MagicMock,
    ) -> None:
        pd_secret = _patch_pd_secret()

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_with_telegram,
            ),
            patch("httpx.post", side_effect=_httpx_post_dispatch) as mock_http_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.telegram.log_event"),
        ):
            route_event("CIRCUIT_BREAKER_OPEN", {"venue": "bybit"})

        pd_calls = [c for c in mock_http_post.call_args_list if "pagerduty.com" in c.args[0]]
        assert pd_calls, "PagerDuty httpx.post was not called"
        pd_json: dict[str, object] = pd_calls[0].kwargs["json"]
        inner = pd_json["payload"]
        assert isinstance(inner, dict)
        assert inner["severity"] == "critical"

    def test_circuit_breaker_venue_detail_propagated(
        self,
        mock_pd_config: MagicMock,
        mock_router_config_with_telegram: MagicMock,
    ) -> None:
        pd_secret = _patch_pd_secret()
        details: dict[str, object] = {"venue": "coinbase", "failure_count": 5}

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_with_telegram,
            ),
            patch("httpx.post", side_effect=_httpx_post_dispatch) as mock_http_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.telegram.log_event"),
        ):
            route_event("CIRCUIT_BREAKER_OPEN", details)

        pd_calls = [c for c in mock_http_post.call_args_list if "pagerduty.com" in c.args[0]]
        assert pd_calls, "PagerDuty httpx.post was not called"
        pd_json: dict[str, object] = pd_calls[0].kwargs["json"]
        inner = pd_json["payload"]
        assert isinstance(inner, dict)
        assert inner["custom_details"] == details

    def test_circuit_breaker_pagerduty_http_failure_does_not_raise(
        self,
        mock_pd_config: MagicMock,
        mock_router_config_with_telegram: MagicMock,
    ) -> None:
        """Route must complete without raising even when PagerDuty returns 500."""
        pd_secret = _patch_pd_secret()

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_with_telegram,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post",
                return_value=_make_http_response(500, "server error"),
            ),
            patch(
                "alerting_service.notifiers.telegram.httpx.post",
                return_value=_make_http_response(200),
            ),
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.telegram.log_event"),
        ):
            route_event("CIRCUIT_BREAKER_OPEN", {"venue": "binance"})
        # No assertion needed -- reaching here without exception is the pass condition.

    def test_circuit_breaker_pagerduty_network_error_does_not_raise(
        self,
        mock_pd_config: MagicMock,
        mock_router_config_with_telegram: MagicMock,
    ) -> None:
        """Route must complete without raising even when httpx raises."""
        pd_secret = _patch_pd_secret()

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_with_telegram,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post",
                side_effect=httpx.ConnectError("refused"),
            ),
            patch(
                "alerting_service.notifiers.telegram.httpx.post",
                return_value=_make_http_response(200),
            ),
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            patch("alerting_service.notifiers.telegram.log_event"),
        ):
            route_event("CIRCUIT_BREAKER_OPEN", {"venue": "binance"})


# ---------------------------------------------------------------------------
# Integration tests: PREFLIGHT_FAILED -> Telegram (or Slack fallback)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPreflightFailedRouting:
    """PREFLIGHT_FAILED must trigger Telegram (or Slack fallback)."""

    def test_preflight_failed_triggers_slack_fallback(
        self,
        mock_slack_config: MagicMock,
        mock_router_config_no_telegram: MagicMock,
    ) -> None:
        slack_secret = _patch_slack_secret()

        with (
            patch(
                "alerting_service.notifiers.slack.UnifiedCloudConfig",
                return_value=mock_slack_config,
            ),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_no_telegram,
            ),
            patch("alerting_service.notifiers.pagerduty.httpx.post") as mock_pd_post,
            patch(
                "alerting_service.notifiers.slack.httpx.post", return_value=_make_http_response(200)
            ) as mock_slack_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("PREFLIGHT_FAILED", {"session": "2026-01-15", "source": "risk-service"})

        mock_slack_post.assert_called_once()
        mock_pd_post.assert_not_called()

    def test_preflight_failed_triggers_telegram_when_configured(
        self,
        mock_router_config_with_telegram: MagicMock,
    ) -> None:
        with (
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_with_telegram,
            ),
            patch("httpx.post", side_effect=_httpx_post_dispatch) as mock_http_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.telegram.log_event"),
        ):
            route_event("PREFLIGHT_FAILED", {"session": "2026-01-15"})

        tg_calls = [c for c in mock_http_post.call_args_list if "api.telegram.org" in c.args[0]]
        slack_calls = [c for c in mock_http_post.call_args_list if "slack.com" in c.args[0]]
        assert tg_calls, "Telegram httpx.post was not called"
        assert not slack_calls, "Slack should not be called when Telegram is configured"

    def test_preflight_failed_slack_text_contains_event_name(
        self,
        mock_slack_config: MagicMock,
        mock_router_config_no_telegram: MagicMock,
    ) -> None:
        slack_secret = _patch_slack_secret()

        with (
            patch(
                "alerting_service.notifiers.slack.UnifiedCloudConfig",
                return_value=mock_slack_config,
            ),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_no_telegram,
            ),
            patch("alerting_service.notifiers.pagerduty.httpx.post"),
            patch(
                "alerting_service.notifiers.slack.httpx.post", return_value=_make_http_response(200)
            ) as mock_slack_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("PREFLIGHT_FAILED", {"session": "2026-01-15"})

        slack_json: dict[str, object] = mock_slack_post.call_args.kwargs["json"]
        assert "PREFLIGHT_FAILED" in str(slack_json["text"])

    def test_preflight_failed_slack_webhook_url_from_secret_manager(
        self,
        mock_slack_config: MagicMock,
        mock_router_config_no_telegram: MagicMock,
    ) -> None:
        webhook_url = "https://hooks.slack.com/services/TINTEGR/BINTEGR/hook-url"
        slack_secret = _patch_slack_secret(webhook_url=webhook_url)

        with (
            patch(
                "alerting_service.notifiers.slack.UnifiedCloudConfig",
                return_value=mock_slack_config,
            ),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_no_telegram,
            ),
            patch("alerting_service.notifiers.pagerduty.httpx.post"),
            patch(
                "alerting_service.notifiers.slack.httpx.post", return_value=_make_http_response(200)
            ) as mock_slack_post,
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("PREFLIGHT_FAILED", {"session": "2026-01-15"})

        called_url = mock_slack_post.call_args[0][0]
        assert called_url == webhook_url

    def test_preflight_failed_slack_failure_does_not_raise(
        self,
        mock_slack_config: MagicMock,
        mock_router_config_no_telegram: MagicMock,
    ) -> None:
        slack_secret = _patch_slack_secret()

        with (
            patch(
                "alerting_service.notifiers.slack.UnifiedCloudConfig",
                return_value=mock_slack_config,
            ),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_no_telegram,
            ),
            patch("alerting_service.notifiers.pagerduty.httpx.post"),
            patch(
                "alerting_service.notifiers.slack.httpx.post",
                return_value=_make_http_response(503, "service unavailable"),
            ),
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("PREFLIGHT_FAILED", {"session": "2026-01-15"})
        # Reaching here without exception is the pass condition.

    def test_preflight_failed_slack_network_error_does_not_raise(
        self,
        mock_slack_config: MagicMock,
        mock_router_config_no_telegram: MagicMock,
    ) -> None:
        slack_secret = _patch_slack_secret()

        with (
            patch(
                "alerting_service.notifiers.slack.UnifiedCloudConfig",
                return_value=mock_slack_config,
            ),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_no_telegram,
            ),
            patch("alerting_service.notifiers.pagerduty.httpx.post"),
            patch(
                "alerting_service.notifiers.slack.httpx.post",
                side_effect=httpx.TimeoutException("timed out"),
            ),
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            route_event("PREFLIGHT_FAILED", {"session": "2026-01-15"})


# ---------------------------------------------------------------------------
# Integration tests: missing secret -> RuntimeError propagates
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSecretManagerMissingSecret:
    """When Secret Manager returns None for a secret, RuntimeError must propagate."""

    def test_missing_pd_secret_raises_runtime_error(
        self,
        mock_pd_config: MagicMock,
        mock_router_config_with_telegram: MagicMock,
    ) -> None:
        pd_secret = MagicMock()
        pd_secret.get_secret.return_value = None  # secret not found

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig",
                return_value=mock_pd_config,
            ),
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=pd_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_with_telegram,
            ),
            patch("alerting_service.notifiers.pagerduty.httpx.post"),
            patch(
                "alerting_service.notifiers.telegram.httpx.post",
                return_value=_make_http_response(200),
            ),
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.telegram.log_event"),
            pytest.raises(RuntimeError, match="alerting-pagerduty-routing-key"),
        ):
            route_event("CIRCUIT_BREAKER_OPEN", {"venue": "binance"})

    def test_missing_slack_secret_raises_runtime_error(
        self,
        mock_slack_config: MagicMock,
        mock_router_config_no_telegram: MagicMock,
    ) -> None:
        slack_secret = MagicMock()
        slack_secret.get_secret.return_value = None  # secret not found

        with (
            patch(
                "alerting_service.notifiers.slack.UnifiedCloudConfig",
                return_value=mock_slack_config,
            ),
            patch("alerting_service.notifiers.slack.get_secret_client", return_value=slack_secret),
            patch(
                "alerting_service.notifiers.router.AlertingSystemConfig",
                return_value=mock_router_config_no_telegram,
            ),
            patch("alerting_service.notifiers.pagerduty.httpx.post"),
            patch("alerting_service.notifiers.slack.httpx.post"),
            patch("alerting_service.notifiers.router.log_event"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            pytest.raises(RuntimeError, match="alerting-slack-webhook-url"),
        ):
            route_event("PREFLIGHT_FAILED", {"session": "2026-01-15"})
