"""Unit tests for the UTS Live Alerts → Slack mirror notifier."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alerting_service.notifiers.uts_live_alerts_slack import (
    _build_blocks,
    _emoji_for,
    send_uts_live_alert,
)

pytestmark = pytest.mark.unit

_WEBHOOK = "https://hooks.slack.com/services/T/B/secret"


def _resp(status: int, text: str = "ok") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    return resp


class TestEmojiSelection:
    def test_severity_wins(self) -> None:
        assert _emoji_for("WHATEVER", "critical") == ":rotating_light:"
        assert _emoji_for("WHATEVER", "warning") == ":warning:"

    def test_event_name_fallback(self) -> None:
        assert _emoji_for("KILL_SWITCH_ACTIVATED", None) == ":rotating_light:"
        assert _emoji_for("PREFLIGHT_FAILED", None) == ":x:"
        assert _emoji_for("SERVICE_DEGRADED", None) == ":warning:"


class TestBuildBlocks:
    def test_includes_event_and_enriched_fields(self) -> None:
        blocks = _build_blocks(
            "PREFLIGHT_FAILED",
            "[PREFLIGHT_FAILED] venue auth ping failed",
            {"severity": "high", "venue": "binance", "service": "execution-service"},
        )
        flat = str(blocks)
        assert "UTS Live Alert" in flat
        assert "PREFLIGHT_FAILED" in flat
        assert "binance" in flat
        assert "execution-service" in flat


class TestSendUtsLiveAlert:
    def test_noop_on_empty_webhook(self) -> None:
        with patch("alerting_service.notifiers.uts_live_alerts_slack.httpx.post") as mock_post:
            assert send_uts_live_alert("", "PREFLIGHT_FAILED", "x", {}) is False
            mock_post.assert_not_called()

    def test_success_posts_once(self) -> None:
        with patch(
            "alerting_service.notifiers.uts_live_alerts_slack.httpx.post",
            return_value=_resp(200),
        ) as mock_post:
            assert send_uts_live_alert(_WEBHOOK, "PREFLIGHT_FAILED", "x", {}) is True
            mock_post.assert_called_once()
            assert mock_post.call_args.args[0] == _WEBHOOK

    def test_4xx_does_not_retry(self) -> None:
        with patch(
            "alerting_service.notifiers.uts_live_alerts_slack.httpx.post",
            return_value=_resp(404, "no_service"),
        ) as mock_post:
            assert send_uts_live_alert(_WEBHOOK, "PREFLIGHT_FAILED", "x", {}) is False
            assert mock_post.call_count == 1

    def test_5xx_retries_then_fails(self) -> None:
        with (
            patch(
                "alerting_service.notifiers.uts_live_alerts_slack.httpx.post",
                return_value=_resp(503),
            ) as mock_post,
            patch("alerting_service.notifiers.uts_live_alerts_slack.time.sleep"),
        ):
            assert send_uts_live_alert(_WEBHOOK, "PREFLIGHT_FAILED", "x", {}) is False
            assert mock_post.call_count == 3

    def test_http_error_is_swallowed(self) -> None:
        with (
            patch(
                "alerting_service.notifiers.uts_live_alerts_slack.httpx.post",
                side_effect=httpx.ConnectError("boom"),
            ),
            patch("alerting_service.notifiers.uts_live_alerts_slack.time.sleep"),
        ):
            # Never raises; returns False after exhausting retries.
            assert send_uts_live_alert(_WEBHOOK, "PREFLIGHT_FAILED", "x", {}) is False
