"""Router tests: DP_RUN_MOSTLY_EMPTY STATIC BACKLOG paging-cadence downgrade.

Ruling (``cefi_high_attempted_failed_batch_cluster_2026_07_23.md`` [BACKEND] P2
todo, 2026-07-28): once a cell is labeled STATIC BACKLOG (deployment-service's
``attempted_failed_staleness.py`` stamps ``details["is_static_backlog"]``), the
30-min CRITICAL re-page is suppressed and downgraded to a WARN-tier,
Slack-only reminder on a daily cooldown — never full silence. The instant a
cell shows fresh (non-static) activity again, CRITICAL paging resumes
immediately, unsuppressed.

Asserts end-to-end via ``route_event`` (block-network: every external sink is
mocked), mirroring the pattern in ``test_router_deployment_enrichment.py``:
  - a STATIC BACKLOG DP_RUN_MOSTLY_EMPTY does NOT page PagerDuty/Telegram, but
    STILL mirrors to #data-pipeline-alerts (the downgraded reminder);
  - a fresh (non-static) DP_RUN_MOSTLY_EMPTY pages exactly as before
    (unsuppressed CRITICAL -> PagerDuty + Telegram + Slack mirror).
"""

from unittest.mock import MagicMock, patch

import pytest

from alerting_service.notifiers.router import route_event

pytestmark = pytest.mark.unit

_DP_WEBHOOK = "https://hooks.slack.com/services/T/B/dp"


def _make_cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.telegram_bot_token = "bot-token"
    cfg.telegram_chat_id = "chat"
    cfg.telegram_chat_id_ops = ""
    cfg.uts_live_alerts_slack_webhook = ""
    cfg.data_pipeline_slack_webhook = _DP_WEBHOOK
    cfg.deployment_ui_base_url = ""
    cfg.deployment_scripts_log_bucket = ""
    cfg.gcp_project_id = "test-project"
    cfg.pagerduty_disabled = False
    cfg.quietness_baseline_mode = False
    cfg.routing_rules = [{"event_pattern": "*", "channels": ["telegram"], "severity_filter": None}]
    return cfg


def _run(details: dict[str, object]):
    """Route a DP_RUN_MOSTLY_EMPTY event with all external sinks mocked."""
    with (
        patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=_make_cfg()),
        patch("alerting_service.notifiers.router.get_paging_credentials", return_value={}),
        patch("alerting_service.notifiers.router._persist_config_snapshot"),
        patch("alerting_service.notifiers.router._persist_delivery_record"),
        patch("alerting_service.notifiers.router._publish_kill_switch_event"),
        patch("alerting_service.notifiers.router.send_data_pipeline_alert", return_value=True) as mock_dp,
        patch("alerting_service.notifiers.router.pd_send_event", return_value=True) as mock_pd,
        patch("alerting_service.notifiers.router.send_uts_live_alert") as mock_slack,
    ):
        route_event("DP_RUN_MOSTLY_EMPTY", details)
        return mock_dp, mock_pd, mock_slack


class TestStaticBacklogSuppressesPagingNotSlack:
    def test_static_backlog_cell_does_not_page(self) -> None:
        mock_dp, mock_pd, _mock_slack = _run(
            {
                "asset_group": "cefi/options_chain",
                "asset_group_name": "cefi",
                "data_type": "options_chain",
                "attempted_failed": 113593,
                "captured": 2,
                "ratio": 1.0,
                "stale_days": 9,
                "is_static_backlog": True,
            }
        )
        # Still mirrors to #data-pipeline-alerts — a downgraded reminder, not silence.
        mock_dp.assert_called_once()
        assert mock_dp.call_args.args[1] == "DP_RUN_MOSTLY_EMPTY"
        # But does NOT page PagerDuty/Telegram.
        mock_pd.assert_not_called()

    def test_static_backlog_slack_message_carries_warn_severity(self) -> None:
        mock_dp, _mock_pd, _mock_slack = _run(
            {
                "asset_group": "cefi/futures_chain",
                "data_type": "futures_chain",
                "stale_days": 6,
                "is_static_backlog": True,
            }
        )
        details_sent = mock_dp.call_args.args[3]
        assert details_sent["severity"] == "WARN"

    def test_fresh_cell_still_pages_as_before(self) -> None:
        mock_dp, mock_pd, _mock_slack = _run(
            {
                "asset_group": "cefi/liquidations",
                "data_type": "liquidations",
                "stale_days": 0,
                "is_static_backlog": False,
            }
        )
        mock_dp.assert_called_once()
        mock_pd.assert_called()
        details_sent = mock_dp.call_args.args[3]
        assert details_sent["severity"] == "CRITICAL"

    def test_missing_is_static_backlog_field_still_pages(self) -> None:
        """Back-compat: an emitter payload predating the staleness label (or a
        cell attempted_failed_staleness couldn't compute, e.g. stale_days=None)
        must not accidentally suppress paging — absence is NOT STATIC BACKLOG."""
        mock_dp, mock_pd, _mock_slack = _run(
            {
                "asset_group": "cefi/trades",
                "data_type": "trades",
            }
        )
        mock_dp.assert_called_once()
        mock_pd.assert_called()
