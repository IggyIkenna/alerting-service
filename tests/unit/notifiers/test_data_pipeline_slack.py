"""Unit tests for the data-pipeline-alerts → Slack mirror notifier."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alerting_service.notifiers.data_pipeline_slack import (
    _build_blocks,
    _emoji_for,
    send_data_pipeline_alert,
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
        assert _emoji_for("WHATEVER", "warn") == ":warning:"
        assert _emoji_for("WHATEVER", "info") == ":information_source:"

    def test_event_name_fallback(self) -> None:
        assert _emoji_for("DP_UNPROVEN_HONEST_ABSENCE", None) == ":rotating_light:"
        assert _emoji_for("DP_VM_EXIT_NONZERO", None) == ":rotating_light:"
        assert _emoji_for("CONSOLIDATOR_DOWN", None) == ":x:"
        assert _emoji_for("DP_VM_STALL", None) == ":warning:"


class TestBuildBlocks:
    def test_includes_event_and_enriched_fields(self) -> None:
        blocks = _build_blocks(
            "DP_UNPROVEN_HONEST_ABSENCE",
            "[DP_UNPROVEN_HONEST_ABSENCE] 401 stamped as empty",
            {"severity": "CRITICAL", "asset_group": "defi", "source": "thegraph", "venue": "aave_v3"},
        )
        flat = str(blocks)
        assert "Data-Pipeline Alert" in flat
        assert "DP_UNPROVEN_HONEST_ABSENCE" in flat
        assert "defi" in flat
        assert "thegraph" in flat
        assert "CRITICAL" in flat


class TestSendDataPipelineAlert:
    def test_noop_on_empty_webhook(self) -> None:
        with patch("alerting_service.notifiers.data_pipeline_slack.httpx.post") as mock_post:
            assert send_data_pipeline_alert("", "DP_VM_STALL", "x", {}) is False
            mock_post.assert_not_called()

    def test_success_posts_once(self) -> None:
        with patch(
            "alerting_service.notifiers.data_pipeline_slack.httpx.post",
            return_value=_resp(200),
        ) as mock_post:
            assert send_data_pipeline_alert(_WEBHOOK, "DP_VM_STALL", "x", {}) is True
            mock_post.assert_called_once()
            assert mock_post.call_args.args[0] == _WEBHOOK

    def test_4xx_does_not_retry(self) -> None:
        with patch(
            "alerting_service.notifiers.data_pipeline_slack.httpx.post",
            return_value=_resp(404, "no_service"),
        ) as mock_post:
            assert send_data_pipeline_alert(_WEBHOOK, "DP_VM_STALL", "x", {}) is False
            assert mock_post.call_count == 1

    def test_5xx_retries_then_fails(self) -> None:
        with (
            patch(
                "alerting_service.notifiers.data_pipeline_slack.httpx.post",
                return_value=_resp(503),
            ) as mock_post,
            patch("alerting_service.notifiers.data_pipeline_slack.time.sleep"),
        ):
            assert send_data_pipeline_alert(_WEBHOOK, "DP_VM_STALL", "x", {}) is False
            assert mock_post.call_count == 3

    def test_http_error_is_swallowed(self) -> None:
        with (
            patch(
                "alerting_service.notifiers.data_pipeline_slack.httpx.post",
                side_effect=httpx.ConnectError("boom"),
            ),
            patch("alerting_service.notifiers.data_pipeline_slack.time.sleep"),
        ):
            # Never raises; returns False after exhausting retries.
            assert send_data_pipeline_alert(_WEBHOOK, "DP_VM_STALL", "x", {}) is False
