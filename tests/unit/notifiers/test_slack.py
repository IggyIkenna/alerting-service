"""Unit tests for the Slack notifier."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alerting_service.notifiers.slack import send_message


@pytest.fixture
def mock_secret_client():
    """Patch get_secret_client to return a fixed webhook URL."""
    mock_client = MagicMock()
    mock_client.get_secret.return_value = "https://hooks.slack.com/services/T000/B000/xxxx"
    with patch(
        "alerting_service.notifiers.slack.get_secret_client", return_value=mock_client
    ) as mock:
        yield mock


@pytest.fixture
def mock_config():
    """Patch UnifiedCloudConfig to provide a fixed project_id."""
    mock_cfg = MagicMock()
    mock_cfg.gcp_project_id = "test-project"
    with patch("alerting_service.notifiers.slack.UnifiedCloudConfig", return_value=mock_cfg):
        yield mock_cfg


@pytest.fixture
def mock_log_event():
    """Suppress log_event calls."""
    with patch("alerting_service.notifiers.slack.log_event") as mock:
        yield mock


def _make_response(status_code: int, text: str = "ok") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    return resp


class TestSendMessage:
    def test_returns_true_on_200(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
        ) as mock_post:
            result = send_message(text="Pipeline failed")

        assert result is True
        mock_post.assert_called_once()

    def test_posts_to_webhook_url(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
        ) as mock_post:
            send_message(text="test")

        call_url = mock_post.call_args[0][0]
        assert call_url == "https://hooks.slack.com/services/T000/B000/xxxx"

    def test_payload_contains_text(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
        ) as mock_post:
            send_message(text="Preflight failed for session s1")

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert json_payload["text"] == "Preflight failed for session s1"

    def test_channel_included_when_provided(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
        ) as mock_post:
            send_message(text="test", channel="#pipeline-alerts")

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert json_payload["channel"] == "#pipeline-alerts"

    def test_channel_omitted_when_none(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
        ) as mock_post:
            send_message(text="test")

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert "channel" not in json_payload

    def test_blocks_included_when_provided(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        blocks: list[dict[str, object]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "hello"}}
        ]
        with patch(
            "alerting_service.notifiers.slack.httpx.post", return_value=_make_response(200)
        ) as mock_post:
            send_message(text="fallback", blocks=blocks)

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert json_payload["blocks"] == blocks

    def test_returns_false_on_non_200(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.slack.httpx.post",
            return_value=_make_response(500, "internal error"),
        ):
            result = send_message(text="test")

        assert result is False

    def test_returns_false_on_http_error_and_does_not_raise(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.slack.httpx.post",
            side_effect=httpx.TimeoutException("timed out"),
        ):
            result = send_message(text="test")

        assert result is False
