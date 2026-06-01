"""Unit tests for the Telegram notifier."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alerting_service.notifiers.telegram import send_telegram


@pytest.fixture
def mock_log_event():
    """Suppress log_event calls."""
    with patch("alerting_service.notifiers.telegram.log_event") as mock:
        yield mock


def _make_response(status_code: int, text: str = "") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    return resp


class TestSendTelegram:
    def test_returns_true_on_200(self, mock_log_event: MagicMock) -> None:
        with patch("alerting_service.notifiers.telegram.httpx.post", return_value=_make_response(200)) as mock_post:
            result = send_telegram(message="Alert fired", bot_token="bot123", chat_id="chat456")

        assert result is True
        mock_post.assert_called_once()

    def test_posts_to_correct_url(self, mock_log_event: MagicMock) -> None:
        with patch("alerting_service.notifiers.telegram.httpx.post", return_value=_make_response(200)) as mock_post:
            send_telegram(message="test", bot_token="mytoken", chat_id="123")

        call_url = mock_post.call_args[0][0]
        assert call_url == "https://api.telegram.org/botmytoken/sendMessage"

    def test_payload_contains_chat_id_and_text(self, mock_log_event: MagicMock) -> None:
        with patch("alerting_service.notifiers.telegram.httpx.post", return_value=_make_response(200)) as mock_post:
            send_telegram(message="Kill switch fired", bot_token="t", chat_id="c")

        json_payload: dict[str, str] = mock_post.call_args.kwargs["json"]
        assert json_payload["chat_id"] == "c"
        assert json_payload["text"] == "Kill switch fired"
        assert json_payload["parse_mode"] == "HTML"

    def test_returns_false_on_non_200(self, mock_log_event: MagicMock) -> None:
        with patch(
            "alerting_service.notifiers.telegram.httpx.post",
            return_value=_make_response(400, "bad request"),
        ):
            result = send_telegram(message="test", bot_token="t", chat_id="c")

        assert result is False

    def test_returns_false_on_http_error_and_does_not_raise(self, mock_log_event: MagicMock) -> None:
        with patch(
            "alerting_service.notifiers.telegram.httpx.post",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            result = send_telegram(message="test", bot_token="t", chat_id="c")

        assert result is False

    def test_logs_event_on_success(self, mock_log_event: MagicMock) -> None:
        with patch("alerting_service.notifiers.telegram.httpx.post", return_value=_make_response(200)):
            send_telegram(message="test msg", bot_token="t", chat_id="c")

        mock_log_event.assert_called_once()
        assert mock_log_event.call_args[0][0] == "TELEGRAM_MESSAGE_SENT"
