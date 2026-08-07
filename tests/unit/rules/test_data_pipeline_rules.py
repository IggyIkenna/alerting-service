"""Unit tests for data-pipeline alert routing (DP_* family + CONSOLIDATOR_DOWN).

Covers:
  * the ``data_pipeline_rule_for`` lookup (exact-match, unmatched → None);
  * ``router.route_event`` for a DP_* CRITICAL event → mirror to
    data_pipeline_slack + the CRITICAL incident path (PagerDuty + Telegram);
  * a DP_* WARN event → mirror only, deduped within the TTL window;
  * an unmatched event does NOT mirror to data_pipeline_slack.

The webhook POST is mocked (QG runs ``--block-network``).
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from alerting_service.core.dedup import AlertDeduplicator
from alerting_service.notifiers import router
from alerting_service.notifiers.router import route_event
from alerting_service.rules.data_pipeline_rules import (
    data_pipeline_rule_for,
    is_data_pipeline_event,
)

pytestmark = pytest.mark.unit

_WEBHOOK = "https://hooks.slack.com/services/T/B/secret"


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


class TestDataPipelineRuleFor:
    def test_dp_critical_event_matches(self) -> None:
        rule = data_pipeline_rule_for("DP_UNPROVEN_HONEST_ABSENCE")
        assert rule is not None
        assert rule.severity.value == "CRITICAL"

    def test_dp_warn_event_matches(self) -> None:
        rule = data_pipeline_rule_for("DP_VM_STALL")
        assert rule is not None
        assert rule.severity.value == "WARN"

    def test_consolidator_down_is_a_member(self) -> None:
        assert data_pipeline_rule_for("CONSOLIDATOR_DOWN") is not None
        assert is_data_pipeline_event("CONSOLIDATOR_DOWN")

    def test_unmatched_event_returns_none(self) -> None:
        assert data_pipeline_rule_for("KILL_SWITCH_ACTIVATED") is None
        assert not is_data_pipeline_event("KILL_SWITCH_ACTIVATED")

    def test_dp_fleet_monitor_lifecycle_events_are_registered(self) -> None:
        """2026-07-27 regression — dp-fleet-monitor's run_lifecycle() events must be
        exact-match registered so they short-circuit before the generic catch-all."""
        started = data_pipeline_rule_for("DP_FLEET_MONITOR_RUN_STARTED")
        completed = data_pipeline_rule_for("DP_FLEET_MONITOR_RUN_COMPLETED")
        failed = data_pipeline_rule_for("DP_FLEET_MONITOR_RUN_FAILED")

        assert started is not None and started.severity.value == "INFO"
        assert completed is not None and completed.severity.value == "INFO"
        assert failed is not None and failed.severity.value == "CRITICAL"

    def test_source_rate_limit_events_are_registered(self) -> None:
        """2026-07-30 finalize verification — the per-source rate-limit/health events
        emitted by ThegraphKeyPoolRotator + DatabentoIPRateLimiter
        (market-tick-data-service@7f42c557) must be exact-match registered under
        DP-RATE-001/DP-RATE-002 so a 429-storm short-circuits to #data-pipeline-alerts
        instead of falling through to the generic catch-all."""
        rate_limited = data_pipeline_rule_for("DP_SOURCE_RATE_LIMITED")
        pool_exhausted = data_pipeline_rule_for("DP_KEY_POOL_EXHAUSTED")

        assert rate_limited is not None
        assert rate_limited.registry_id == "DP-RATE-001"
        assert rate_limited.severity.value == "WARN"

        assert pool_exhausted is not None
        assert pool_exhausted.registry_id == "DP-RATE-002"
        assert pool_exhausted.severity.value == "CRITICAL"


# ---------------------------------------------------------------------------
# Router integration
# ---------------------------------------------------------------------------


@pytest.fixture
def _fresh_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a fresh module-level deduplicator (no cross-test leakage)."""
    monkeypatch.setattr(router, "_deduplicator", AlertDeduplicator(ttl_seconds=60.0))


@pytest.fixture
def mock_config() -> Iterator[MagicMock]:
    """Patch _get_cloud_config to a config carrying the DP webhook + paging."""
    cfg = MagicMock()
    cfg.data_pipeline_slack_webhook = _WEBHOOK
    cfg.telegram_bot_token = "bot-token-123"
    cfg.telegram_chat_id = "chat-456"
    cfg.telegram_chat_id_ops = ""
    cfg.uts_live_alerts_slack_webhook = ""
    cfg.gcp_project_id = "test-project"
    cfg.pagerduty_disabled = False
    cfg.quietness_baseline_mode = False
    cfg.routing_rules = [{"event_pattern": "*", "channels": ["telegram"], "severity_filter": None}]
    with patch("alerting_service.notifiers.router._get_cloud_config", return_value=cfg):
        yield cfg


@pytest.fixture
def mock_creds() -> Iterator[MagicMock]:
    """Patch get_paging_credentials — no SM webhook (use config fallback)."""
    creds: dict[str, str] = {}
    with patch("alerting_service.notifiers.router.get_paging_credentials", return_value=creds) as mock:
        yield mock


@pytest.fixture
def mock_send_dp() -> Iterator[MagicMock]:
    """Mock the data_pipeline_slack notifier POST (block-network safe)."""
    with patch("alerting_service.notifiers.router.send_data_pipeline_alert", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_explicit_route() -> Iterator[MagicMock]:
    """Mock the CRITICAL incident path (route_event_with_explicit_channels)."""
    with patch("alerting_service.notifiers.router.route_event_with_explicit_channels") as mock:
        yield mock


@pytest.fixture
def mock_persist_and_log() -> Iterator[None]:
    """Suppress log_event + config-snapshot persistence side-effects."""
    with (
        patch("alerting_service.notifiers.router.log_event"),
        patch("alerting_service.notifiers.router._persist_config_snapshot"),
    ):
        yield


@pytest.mark.usefixtures("_fresh_dedup", "mock_config", "mock_creds", "mock_persist_and_log")
class TestRouteEventDataPipeline:
    def test_critical_dp_event_mirrors_and_pages(self, mock_send_dp: MagicMock, mock_explicit_route: MagicMock) -> None:
        route_event(
            "DP_UNPROVEN_HONEST_ABSENCE",
            {"message": "401 stamped as empty", "asset_group": "defi", "source": "thegraph"},
        )

        # Slack mirror fired with the DP webhook + the CRITICAL severity.
        mock_send_dp.assert_called_once()
        assert mock_send_dp.call_args.args[0] == _WEBHOOK
        assert mock_send_dp.call_args.args[1] == "DP_UNPROVEN_HONEST_ABSENCE"
        mirror_details = mock_send_dp.call_args.args[3]
        assert mirror_details["severity"] == "CRITICAL"

        # CRITICAL → incident path (PagerDuty + Telegram), reusing existing plumbing.
        mock_explicit_route.assert_called_once()
        assert mock_explicit_route.call_args.args[0] == "DP_UNPROVEN_HONEST_ABSENCE"
        assert mock_explicit_route.call_args.kwargs["channels"] == {"pagerduty", "telegram"}
        assert mock_explicit_route.call_args.kwargs["pd_severity"] == "critical"

    def test_warn_dp_event_mirrors_only_no_page(self, mock_send_dp: MagicMock, mock_explicit_route: MagicMock) -> None:
        route_event("DP_VM_STALL", {"message": "vm idle", "vm": "vm-defi-1"})

        mock_send_dp.assert_called_once()
        assert mock_send_dp.call_args.args[3]["severity"] == "WARN"
        # WARN does NOT page.
        mock_explicit_route.assert_not_called()

    def test_warn_dp_event_deduped_within_window(self, mock_send_dp: MagicMock, mock_explicit_route: MagicMock) -> None:
        details = {"message": "vm idle", "vm": "vm-defi-1"}
        route_event("DP_VM_STALL", details)
        route_event("DP_VM_STALL", details)  # identical → deduped by AlertDeduplicator

        # Only the first fire reaches the mirror.
        mock_send_dp.assert_called_once()

    def test_dp_run_mostly_empty_collapses_across_meta_sweep_cadence(
        self, mock_send_dp: MagicMock, mock_explicit_route: MagicMock
    ) -> None:
        """2026-07-15 regression — DP_RUN_MOSTLY_EMPTY (CRITICAL) is a static,
        re-scanned-every-tick manifest-cell signal that opted into the recurring
        cooldown (``_RECURRING_ALERT_COOLDOWNS``). Two byte-identical fires 900s
        apart (the observed ``*/15`` meta-sweep gap) MUST collapse to ONE delivered
        alert; a third fire past the 1800s cooldown boundary IS delivered again
        (CRITICAL still re-nags while unresolved — it just stops literally
        duplicating every tick)."""
        details = {
            "asset_group": "sports",
            "data_type": "trades",
            "attempted_failed": 112277,
            "attempted": 522276,
            "ratio": 0.215,
        }

        with patch("alerting_service.core.dedup.time.monotonic", return_value=0.0):
            route_event("DP_RUN_MOSTLY_EMPTY", details)

        # Identical fire 900s later (the real-world 12:03 / 12:17 duplicate) →
        # suppressed as a duplicate, not delivered again.
        with patch("alerting_service.core.dedup.time.monotonic", return_value=900.0):
            route_event("DP_RUN_MOSTLY_EMPTY", details)

        mock_send_dp.assert_called_once()
        mock_explicit_route.assert_called_once()

        # Past the 1800s cooldown the still-unresolved condition re-nags.
        with patch("alerting_service.core.dedup.time.monotonic", return_value=1801.0):
            route_event("DP_RUN_MOSTLY_EMPTY", details)

        assert mock_send_dp.call_count == 2
        assert mock_explicit_route.call_count == 2

    def test_dp_fleet_monitor_run_started_registered_but_not_mirrored(
        self, mock_send_dp: MagicMock, mock_explicit_route: MagicMock
    ) -> None:
        """2026-07-27 regression — routine dp-fleet-monitor telemetry must be exact-match
        REGISTERED (data_pipeline_rule_for matches it, never falls through to the generic
        incident catch-all / wrong channel) and never page. 2026-08-07 update (operator:
        "i just need to know if it failed to complete... and if it doesnt start at all"):
        STARTED/COMPLETED additionally no longer mirror to Slack at all on every ~5min sweep
        tick (DataPipelineAlertRule.mirror_live=False) — the deadman/cron-watches-cron
        sentinel layer already covers "didn't start" independently of this event."""
        route_event("DP_FLEET_MONITOR_RUN_STARTED", {"message": "sweep starting", "run_id": "20260727T000000Z-abc"})

        mock_send_dp.assert_not_called()
        mock_explicit_route.assert_not_called()

    def test_dp_fleet_monitor_run_completed_registered_but_not_mirrored(
        self, mock_send_dp: MagicMock, mock_explicit_route: MagicMock
    ) -> None:
        route_event("DP_FLEET_MONITOR_RUN_COMPLETED", {"message": "sweep done", "elapsed_s": 12.3})

        mock_send_dp.assert_not_called()
        mock_explicit_route.assert_not_called()

    def test_dp_fleet_monitor_run_failed_mirrors_and_pages(
        self, mock_send_dp: MagicMock, mock_explicit_route: MagicMock
    ) -> None:
        """A crash of the monitor itself is meta-incident-worthy — unlike STARTED/
        COMPLETED, _FAILED must page (same tier as DP_ZOMBIE_WATCHDOG_DOWN)."""
        route_event(
            "DP_FLEET_MONITOR_RUN_FAILED",
            {"message": "sweep crashed", "exception_type": "RuntimeError"},
        )

        mock_send_dp.assert_called_once()
        assert mock_send_dp.call_args.args[3]["severity"] == "CRITICAL"
        mock_explicit_route.assert_called_once()
        assert mock_explicit_route.call_args.kwargs["channels"] == {"pagerduty", "telegram"}

    def test_dp_source_rate_limited_injected_429_storm_routes_to_mirror_not_page(
        self, mock_send_dp: MagicMock, mock_explicit_route: MagicMock
    ) -> None:
        """2026-07-30 finalize verification (DP-RATE-001) — an injected 429-storm from
        ThegraphKeyPoolRotator/DatabentoIPRateLimiter (the shape
        market-tick-data-service@7f42c557 emits) must mirror to
        #data-pipeline-alerts and NOT fall through to the generic incident catch-all.
        WARN severity — auto-recover, no page."""
        route_event(
            "DP_SOURCE_RATE_LIMITED",
            {"message": "429 budget exceeded", "source": "databento", "venue": "sync", "http_429_count": 7},
        )

        mock_send_dp.assert_called_once()
        assert mock_send_dp.call_args.args[1] == "DP_SOURCE_RATE_LIMITED"
        assert mock_send_dp.call_args.args[3]["severity"] == "WARN"
        mock_explicit_route.assert_not_called()

    def test_dp_key_pool_exhausted_injected_storm_routes_to_mirror_and_pages(
        self, mock_send_dp: MagicMock, mock_explicit_route: MagicMock
    ) -> None:
        """2026-07-30 finalize verification (DP-RATE-002) — TheGraph's 9-key pool fully
        exhausted must mirror to #data-pipeline-alerts AND page (CRITICAL), same as any
        other DP_* CRITICAL event — proves the exact defect class already fixed for
        DP_FLEET_MONITOR_RUN_* (unified-api-contracts@92e068ea) does not recur here."""
        route_event(
            "DP_KEY_POOL_EXHAUSTED",
            {"message": "all 9 keys rate-limited", "source": "thegraph", "venue": "dex_pools"},
        )

        mock_send_dp.assert_called_once()
        assert mock_send_dp.call_args.args[1] == "DP_KEY_POOL_EXHAUSTED"
        assert mock_send_dp.call_args.args[3]["severity"] == "CRITICAL"
        mock_explicit_route.assert_called_once()
        assert mock_explicit_route.call_args.kwargs["channels"] == {"pagerduty", "telegram"}
        assert mock_explicit_route.call_args.kwargs["pd_severity"] == "critical"

    def test_unmatched_event_does_not_mirror_to_data_pipeline_slack(
        self,
        mock_send_dp: MagicMock,
        mock_explicit_route: MagicMock,
        mock_send_telegram: MagicMock,
    ) -> None:
        route_event("SOME_OTHER_EVENT", {"message": "unrelated"})

        mock_send_dp.assert_not_called()
        mock_explicit_route.assert_not_called()


@pytest.fixture
def mock_send_telegram() -> Iterator[MagicMock]:
    """Patch Slack delivery so the generic fallback path for non-DP events is inert."""
    with patch("alerting_service.notifiers.router.send_uts_live_alert") as mock:
        yield mock
