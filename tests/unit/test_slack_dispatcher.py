"""Unit tests for alerting_service.core.slack_dispatcher."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from unified_api_contracts.internal import AlertEvent

from alerting_service.core.slack_dispatcher import SEVERITY_COLORS, build_slack_blocks


def _make_event(severity: str = "WARNING") -> AlertEvent:
    return AlertEvent(
        alert_id="alert-001",
        rule_id="rule-pnl-drawdown",
        triggered_at=datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC),
        severity=severity,
        message="PnL drawdown exceeded threshold",
        metric_value=0.12,
        threshold=0.10,
        strategy_id="momentum-btc",
        venue="binance",
    )


class TestBuildSlackBlocks:
    def test_returns_dict_with_attachments(self) -> None:
        event = _make_event()
        result = build_slack_blocks(event, "https://dashboard.example.com")
        assert "attachments" in result

    def test_color_matches_severity(self) -> None:
        for severity in ("DEBUG", "INFO", "WARNING", "CRITICAL", "FATAL"):
            event = _make_event(severity=severity)
            result = build_slack_blocks(event, "https://example.com")
            attachment = result["attachments"][0]
            assert attachment["color"] == SEVERITY_COLORS[severity]

    def test_unknown_severity_falls_back_to_gray(self) -> None:
        event = _make_event()
        # Temporarily set unknown severity via model mutation
        modified = event.model_copy(update={"severity": "UNKNOWN"})
        result = build_slack_blocks(modified, "https://example.com")
        attachment = result["attachments"][0]
        assert attachment["color"] == "#808080"

    def test_header_contains_severity_and_message(self) -> None:
        event = _make_event("CRITICAL")
        result = build_slack_blocks(event, "https://example.com")
        blocks = result["attachments"][0]["blocks"]
        header_block = blocks[0]
        assert "[CRITICAL]" in header_block["text"]["text"]
        assert event.message in header_block["text"]["text"]

    def test_dashboard_url_in_actions_block(self) -> None:
        event = _make_event()
        dashboard_url = "https://my-dashboard.io"
        result = build_slack_blocks(event, dashboard_url)
        blocks = result["attachments"][0]["blocks"]
        action_block = blocks[2]
        button_url = action_block["elements"][0]["url"]
        assert dashboard_url in button_url


class TestSendSlackAlert:
    @pytest.mark.asyncio
    async def test_returns_ts_on_success(self) -> None:
        from alerting_service.core.slack_dispatcher import send_slack_alert

        event = _make_event()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"ts": "123456.789"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "alerting_service.core.slack_dispatcher.aiohttp.ClientSession",
            return_value=mock_session,
        ):
            result = await send_slack_alert("https://hooks.slack.com/test", event, "https://dash.io")

        assert result == "123456.789"

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200(self) -> None:
        from alerting_service.core.slack_dispatcher import send_slack_alert

        event = _make_event()
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "alerting_service.core.slack_dispatcher.aiohttp.ClientSession",
            return_value=mock_session,
        ):
            result = await send_slack_alert("https://hooks.slack.com/test", event, "https://dash.io")

        assert result is None
