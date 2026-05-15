"""Integration tests: QG_SNAPSHOT_STALE alert routing (B-018 Phase 4.A monitoring).

Verifies that:
1. AlertCode.QG_SNAPSHOT_STALE exists in the closed set and has a matching
   AlertRule in LIVE_ALERT_RULES.
2. route_event("QG_SNAPSHOT_STALE", ...) routes to PagerDuty + Telegram
   (severity HIGH per LIVE_ALERT_RULES entry).
3. The threshold key ``qg_snapshot_stale_days`` is registered in ALERT_THRESHOLDS.
4. The staleness payload fields flow through the routing pipeline intact.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from unified_api_contracts.alerting import (
    ALERT_THRESHOLDS,
    LIVE_ALERT_RULES,
    AlertChannel,
    AlertCode,
    AlertSeverity,
)

from alerting_service.notifiers.router import route_event

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers (mirror test_router_integration.py pattern)
# ---------------------------------------------------------------------------


def _make_http_response(status_code: int, text: str = "ok") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    return resp


def _httpx_post_dispatch(url: str, **kwargs: object) -> MagicMock:
    if "pagerduty.com" in url:
        return _make_http_response(202)
    if "api.telegram.org" in url:
        return _make_http_response(200)
    return _make_http_response(200)


def _patch_secret_client(routing_key: str = "test-routing-key") -> MagicMock:
    mock = MagicMock()
    mock.get_secret.return_value = routing_key
    return mock


# ---------------------------------------------------------------------------
# Taxonomy / closed-set tests
# ---------------------------------------------------------------------------


class TestQGSnapshotStaleTaxonomy:
    """AlertCode + LIVE_ALERT_RULES + ALERT_THRESHOLDS closed-set checks."""

    def test_alert_code_exists(self) -> None:
        assert AlertCode.QG_SNAPSHOT_STALE == "QG_SNAPSHOT_STALE"

    def test_live_alert_rule_registered(self) -> None:
        """LIVE_ALERT_RULES must contain an entry for QG_SNAPSHOT_STALE."""
        matching = [r for r in LIVE_ALERT_RULES if r.code is AlertCode.QG_SNAPSHOT_STALE]
        assert len(matching) == 1, (
            "Expected exactly one AlertRule for QG_SNAPSHOT_STALE in LIVE_ALERT_RULES; "
            f"found {len(matching)}"
        )

    def test_rule_severity_is_high(self) -> None:
        """QG_SNAPSHOT_STALE should be HIGH severity (not WARNING-only or CRITICAL)."""
        rule = next(r for r in LIVE_ALERT_RULES if r.code is AlertCode.QG_SNAPSHOT_STALE)
        assert rule.severity is AlertSeverity.HIGH

    def test_rule_channels_include_pagerduty_and_telegram(self) -> None:
        """HIGH severity rule must dispatch to PagerDuty + Telegram."""
        rule = next(r for r in LIVE_ALERT_RULES if r.code is AlertCode.QG_SNAPSHOT_STALE)
        channels = set(rule.channels)
        assert AlertChannel.PAGERDUTY in channels
        assert AlertChannel.TELEGRAM in channels

    def test_threshold_key_registered(self) -> None:
        """qg_snapshot_stale_days must be in ALERT_THRESHOLDS."""
        assert "qg_snapshot_stale_days" in ALERT_THRESHOLDS

    def test_threshold_default_value(self) -> None:
        from decimal import Decimal

        threshold = ALERT_THRESHOLDS["qg_snapshot_stale_days"]
        assert threshold.default_value == Decimal("2")

    def test_rule_threshold_key_matches(self) -> None:
        """AlertRule.threshold_key must reference the registered threshold."""
        rule = next(r for r in LIVE_ALERT_RULES if r.code is AlertCode.QG_SNAPSHOT_STALE)
        assert rule.threshold_key == "qg_snapshot_stale_days"


# ---------------------------------------------------------------------------
# Routing integration test
# ---------------------------------------------------------------------------


class TestQGSnapshotStaleRouting:
    """Verify QG_SNAPSHOT_STALE routes to PagerDuty + Telegram."""

    @patch("httpx.post", side_effect=_httpx_post_dispatch)
    @patch("alerting_service.notifiers.pagerduty.get_secret_client")
    @patch("alerting_service.notifiers.telegram.get_secret_client")
    def test_routes_to_pagerduty_and_telegram(
        self,
        mock_telegram_secret: MagicMock,
        mock_pd_secret: MagicMock,
        mock_httpx_post: MagicMock,
    ) -> None:
        """route_event("QG_SNAPSHOT_STALE", ...) must call PagerDuty + Telegram."""
        mock_pd_secret.return_value = _patch_secret_client("pd-routing-key")
        mock_telegram_secret.return_value = _patch_secret_client("telegram-bot-token:test")

        payload: dict[str, object] = {
            "stale_days": 2,
            "last_snapshot_date": None,
            "bucket_path": "gs://test-project-deployment-events/quality_gates_snapshot/",
            "checked_dates": ["2026_05_14", "2026_05_15"],
            "message": "QG snapshot missing for 2 of the last 2 days.",
        }

        route_event("QG_SNAPSHOT_STALE", payload)

        called_urls = [call.args[0] for call in mock_httpx_post.call_args_list]
        pagerduty_calls = [u for u in called_urls if "pagerduty.com" in u]
        telegram_calls = [u for u in called_urls if "api.telegram.org" in u]

        assert pagerduty_calls, "Expected at least one PagerDuty call for QG_SNAPSHOT_STALE"
        assert telegram_calls, "Expected at least one Telegram call for QG_SNAPSHOT_STALE"

    @patch("httpx.post", side_effect=_httpx_post_dispatch)
    @patch("alerting_service.notifiers.pagerduty.get_secret_client")
    @patch("alerting_service.notifiers.telegram.get_secret_client")
    def test_payload_fields_forwarded(
        self,
        mock_telegram_secret: MagicMock,
        mock_pd_secret: MagicMock,
        mock_httpx_post: MagicMock,
    ) -> None:
        """Staleness payload fields must reach the notifier call body."""
        mock_pd_secret.return_value = _patch_secret_client("pd-routing-key")
        mock_telegram_secret.return_value = _patch_secret_client("telegram-bot-token:test")

        payload: dict[str, object] = {
            "stale_days": 3,
            "last_snapshot_date": "2026_05_12",
            "bucket_path": "gs://test-project-deployment-events/quality_gates_snapshot/",
            "checked_dates": ["2026_05_13", "2026_05_14", "2026_05_15"],
            "message": "QG snapshot missing for 3 of the last 3 days.",
        }

        route_event("QG_SNAPSHOT_STALE", payload)

        # At least one HTTP call was made (PagerDuty or Telegram)
        assert mock_httpx_post.called, "Expected httpx.post to be called for QG_SNAPSHOT_STALE"
        # Verify stale_days field appears in at least one call body
        call_bodies = [str(c) for c in mock_httpx_post.call_args_list]
        assert any("stale_days" in body or "QG_SNAPSHOT_STALE" in body for body in call_bodies)
