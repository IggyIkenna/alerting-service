"""Integration tests for individual notifiers.

These tests exercise each notifier's send path against a mocked httpx
client and a mocked Secret Manager, validating the full call chain from
send_event / send_message through to the HTTP call — without any real
network I/O.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alerting_service.notifiers.pagerduty import _PAGERDUTY_ENQUEUE_URL, send_event
from alerting_service.notifiers.slack import send_message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int, text: str = "ok") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# Fixtures — PagerDuty
# ---------------------------------------------------------------------------


@pytest.fixture
def pd_secret_client() -> MagicMock:
    mock_client = MagicMock()
    mock_client.get_secret.return_value = "integration-pd-routing-key"
    return mock_client


@pytest.fixture
def pd_config() -> MagicMock:
    mock_cfg = MagicMock()
    mock_cfg.gcp_project_id = "int-test-project"
    return mock_cfg


# ---------------------------------------------------------------------------
# Fixtures — Slack
# ---------------------------------------------------------------------------


@pytest.fixture
def slack_secret_client() -> MagicMock:
    mock_client = MagicMock()
    mock_client.get_secret.return_value = "https://hooks.slack.com/services/T999/B999/int-webhook"
    return mock_client


@pytest.fixture
def slack_config() -> MagicMock:
    mock_cfg = MagicMock()
    mock_cfg.gcp_project_id = "int-test-project"
    return mock_cfg


# ---------------------------------------------------------------------------
# PagerDuty notifier integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPagerDutyNotifierIntegration:
    """End-to-end tests for PagerDutyNotifier.send_event()."""

    def test_send_event_returns_true_on_202(
        self,
        pd_secret_client: MagicMock,
        pd_config: MagicMock,
    ) -> None:
        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=pd_secret_client,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
            ) as mock_post,
            patch("alerting_service.notifiers.pagerduty.log_event"),
        ):
            result = send_event(
                summary="Kill switch activated",
                severity="critical",
                source="execution-service",
                details={"strategy": "momentum-v1"},
            )

        assert result is True
        mock_post.assert_called_once()

    def test_send_event_posts_to_enqueue_url(
        self,
        pd_secret_client: MagicMock,
        pd_config: MagicMock,
    ) -> None:
        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=pd_secret_client,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
            ) as mock_post,
            patch("alerting_service.notifiers.pagerduty.log_event"),
        ):
            send_event(summary="test", severity="info", source="alerting-service", details={})

        assert mock_post.call_args[0][0] == _PAGERDUTY_ENQUEUE_URL

    def test_send_event_payload_contains_routing_key(
        self,
        pd_secret_client: MagicMock,
        pd_config: MagicMock,
    ) -> None:
        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=pd_secret_client,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
            ) as mock_post,
            patch("alerting_service.notifiers.pagerduty.log_event"),
        ):
            send_event(summary="test", severity="warning", source="alerting-service", details={})

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert json_payload["routing_key"] == "integration-pd-routing-key"

    def test_send_event_payload_structure(
        self,
        pd_secret_client: MagicMock,
        pd_config: MagicMock,
    ) -> None:
        details: dict[str, object] = {"venue": "binance", "failure_count": 5}
        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=pd_secret_client,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
            ) as mock_post,
            patch("alerting_service.notifiers.pagerduty.log_event"),
        ):
            send_event(
                summary="Circuit breaker opened on binance",
                severity="critical",
                source="execution-service",
                details=details,
            )

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert json_payload["event_action"] == "trigger"
        inner = json_payload["payload"]
        assert isinstance(inner, dict)
        assert inner["summary"] == "Circuit breaker opened on binance"
        assert inner["severity"] == "critical"
        assert inner["source"] == "execution-service"
        assert inner["custom_details"] == details

    def test_send_event_returns_false_on_400(
        self,
        pd_secret_client: MagicMock,
        pd_config: MagicMock,
    ) -> None:
        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=pd_secret_client,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post",
                return_value=_make_response(400, "bad request"),
            ),
            patch("alerting_service.notifiers.pagerduty.log_event"),
        ):
            result = send_event(
                summary="test",
                severity="error",
                source="alerting-service",
                details={},
            )

        assert result is False

    def test_send_event_returns_false_on_429_rate_limited(
        self,
        pd_secret_client: MagicMock,
        pd_config: MagicMock,
    ) -> None:
        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=pd_secret_client,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post",
                return_value=_make_response(429, "rate limited"),
            ),
            patch("alerting_service.notifiers.pagerduty.log_event"),
        ):
            result = send_event(
                summary="test",
                severity="warning",
                source="alerting-service",
                details={},
            )

        assert result is False

    def test_send_event_returns_false_on_500(
        self,
        pd_secret_client: MagicMock,
        pd_config: MagicMock,
    ) -> None:
        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=pd_secret_client,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post",
                return_value=_make_response(500, "internal server error"),
            ),
            patch("alerting_service.notifiers.pagerduty.log_event"),
        ):
            result = send_event(
                summary="test",
                severity="error",
                source="alerting-service",
                details={},
            )

        assert result is False

    def test_send_event_returns_false_on_connect_error(
        self,
        pd_secret_client: MagicMock,
        pd_config: MagicMock,
    ) -> None:
        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=pd_secret_client,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post",
                side_effect=httpx.ConnectError("connection refused"),
            ),
            patch("alerting_service.notifiers.pagerduty.log_event"),
        ):
            result = send_event(
                summary="test",
                severity="critical",
                source="alerting-service",
                details={},
            )

        assert result is False

    def test_send_event_returns_false_on_timeout(
        self,
        pd_secret_client: MagicMock,
        pd_config: MagicMock,
    ) -> None:
        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=pd_secret_client,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post",
                side_effect=httpx.TimeoutException("request timed out"),
            ),
            patch("alerting_service.notifiers.pagerduty.log_event"),
        ):
            result = send_event(
                summary="test",
                severity="critical",
                source="alerting-service",
                details={},
            )

        assert result is False

    def test_send_event_secret_fetched_with_correct_project(
        self,
        pd_secret_client: MagicMock,
        pd_config: MagicMock,
    ) -> None:
        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=pd_secret_client,
            ) as mock_sm,
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
            ),
            patch("alerting_service.notifiers.pagerduty.log_event"),
        ):
            send_event(summary="test", severity="info", source="alerting-service", details={})

        mock_sm.assert_called_once_with(project_id="int-test-project")
        pd_secret_client.get_secret.assert_called_once_with("alerting-pagerduty-routing-key")

    def test_send_event_raises_when_secret_missing(
        self,
        pd_config: MagicMock,
    ) -> None:
        missing_secret = MagicMock()
        missing_secret.get_secret.return_value = None

        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=missing_secret,
            ),
            patch("alerting_service.notifiers.pagerduty.httpx.post"),
            patch("alerting_service.notifiers.pagerduty.log_event"),
            pytest.raises(RuntimeError, match="alerting-pagerduty-routing-key"),
        ):
            send_event(summary="test", severity="info", source="alerting-service", details={})

    @pytest.mark.parametrize("severity", ["critical", "error", "warning", "info"])
    def test_send_event_all_valid_severities_accepted(
        self,
        severity: str,
        pd_secret_client: MagicMock,
        pd_config: MagicMock,
    ) -> None:
        with (
            patch(
                "alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=pd_config
            ),
            patch(
                "alerting_service.notifiers.pagerduty.get_secret_client",
                return_value=pd_secret_client,
            ),
            patch(
                "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
            ) as mock_post,
            patch("alerting_service.notifiers.pagerduty.log_event"),
        ):
            result = send_event(
                summary="test", severity=severity, source="alerting-service", details={}
            )

        assert result is True
        pd_json: dict[str, object] = mock_post.call_args.kwargs["json"]
        inner = pd_json["payload"]
        assert isinstance(inner, dict)
        assert inner["severity"] == severity


# ---------------------------------------------------------------------------
# Slack notifier integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSlackNotifierIntegration:
    """End-to-end tests for SlackNotifier.send_message()."""

    def test_send_message_returns_true_on_200(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
            ) as mock_post,
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            result = send_message(text="Preflight failed for session 2026-01-15")

        assert result is True
        mock_post.assert_called_once()

    def test_send_message_posts_to_webhook_url_from_secret(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
            ) as mock_post,
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            send_message(text="test")

        called_url = mock_post.call_args[0][0]
        assert called_url == "https://hooks.slack.com/services/T999/B999/int-webhook"

    def test_send_message_payload_contains_text(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
            ) as mock_post,
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            send_message(text="Service degraded: market-data-service latency > 1s")

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert json_payload["text"] == "Service degraded: market-data-service latency > 1s"

    def test_send_message_includes_channel_when_provided(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
            ) as mock_post,
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            send_message(text="test", channel="#pipeline-alerts")

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert json_payload["channel"] == "#pipeline-alerts"

    def test_send_message_omits_channel_when_none(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
            ) as mock_post,
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            send_message(text="test")

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert "channel" not in json_payload

    def test_send_message_includes_blocks_when_provided(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        blocks: list[dict[str, object]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*PREFLIGHT_FAILED*"}},
            {"type": "divider"},
        ]
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
            ) as mock_post,
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            send_message(text="Preflight failed", blocks=blocks)

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert json_payload["blocks"] == blocks

    def test_send_message_omits_blocks_when_none(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
            ) as mock_post,
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            send_message(text="test", blocks=None)

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert "blocks" not in json_payload

    def test_send_message_returns_false_on_non_200(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post",
                return_value=_make_response(400, "invalid_payload"),
            ),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            result = send_message(text="test")

        assert result is False

    def test_send_message_returns_false_on_500(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post",
                return_value=_make_response(500, "internal error"),
            ),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            result = send_message(text="test")

        assert result is False

    def test_send_message_returns_false_on_timeout(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post",
                side_effect=httpx.TimeoutException("timed out"),
            ),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            result = send_message(text="test")

        assert result is False

    def test_send_message_returns_false_on_connect_error(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ),
            patch(
                "alerting_service.notifiers.slack.httpx.post",
                side_effect=httpx.ConnectError("connection refused"),
            ),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            result = send_message(text="test")

        assert result is False

    def test_send_message_secret_fetched_with_correct_project(
        self,
        slack_secret_client: MagicMock,
        slack_config: MagicMock,
    ) -> None:
        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client",
                return_value=slack_secret_client,
            ) as mock_sm,
            patch("alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)),
            patch("alerting_service.notifiers.slack.log_event"),
        ):
            send_message(text="test")

        mock_sm.assert_called_once_with(project_id="int-test-project")
        slack_secret_client.get_secret.assert_called_once_with("alerting-slack-webhook-url")

    def test_send_message_raises_when_secret_missing(
        self,
        slack_config: MagicMock,
    ) -> None:
        missing_secret = MagicMock()
        missing_secret.get_secret.return_value = None

        with (
            patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=slack_config),
            patch(
                "alerting_service.notifiers.slack.get_secret_client", return_value=missing_secret
            ),
            patch("alerting_service.notifiers.slack.httpx.post"),
            patch("alerting_service.notifiers.slack.log_event"),
            pytest.raises(RuntimeError, match="alerting-slack-webhook-url"),
        ):
            send_message(text="test")
