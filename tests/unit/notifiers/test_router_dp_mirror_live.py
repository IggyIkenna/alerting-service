"""Router tests: DataPipelineAlertRule.mirror_live gates the live Slack/page dispatch.

2026-08-07 fix (operator: "i just need to know if it failed to complete... and
if it doesnt start at all"): DP_FLEET_MONITOR_RUN_STARTED/_COMPLETED (DP-DIGEST-003/004)
fire on every ~5min fleet-monitor sweep tick and were mirroring to
#data-pipeline-alerts unconditionally, spamming the channel with routine
telemetry the operator explicitly said they didn't want live. The events stay
registered (data_pipeline_rule_for() still exact-matches them, so they never
fall through to the wrong-channel catch-all — the 2026-07-27 regression class),
but the router must now skip the actual mirror/page dispatch for any rule with
``mirror_live is False`` while still logging ALERT_SENT for audit/metrics.
DP_FLEET_MONITOR_RUN_FAILED (a real incident) must be unaffected.

Asserts end-to-end via ``route_event`` (block-network: every external sink is
mocked), mirroring the pattern in ``test_router_dp_run_mostly_empty_static_backlog.py``.
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


def _run(event_name: str, details: dict[str, object]):
    """Route an event with all external sinks mocked; also captures log_event."""
    with (
        patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=_make_cfg()),
        patch("alerting_service.notifiers.router.get_paging_credentials", return_value={}),
        patch("alerting_service.notifiers.router._persist_config_snapshot"),
        patch("alerting_service.notifiers.router._persist_delivery_record"),
        patch("alerting_service.notifiers.router._publish_kill_switch_event"),
        patch("alerting_service.notifiers.router.send_data_pipeline_alert", return_value=True) as mock_dp,
        patch("alerting_service.notifiers.router.pd_send_event", return_value=True) as mock_pd,
        patch("alerting_service.notifiers.router.send_uts_live_alert") as mock_slack,
        patch("alerting_service.notifiers.router.log_event") as mock_log_event,
    ):
        route_event(event_name, details)
        return mock_dp, mock_pd, mock_slack, mock_log_event


class TestMirrorLiveFalseSuppressesDispatchNotTracking:
    def test_run_started_does_not_mirror_to_slack(self) -> None:
        mock_dp, mock_pd, _mock_slack, _mock_log = _run("DP_FLEET_MONITOR_RUN_STARTED", {})
        mock_dp.assert_not_called()
        mock_pd.assert_not_called()

    def test_run_completed_does_not_mirror_to_slack(self) -> None:
        mock_dp, mock_pd, _mock_slack, _mock_log = _run("DP_FLEET_MONITOR_RUN_COMPLETED", {})
        mock_dp.assert_not_called()
        mock_pd.assert_not_called()

    def test_run_started_still_logs_alert_sent_for_audit(self) -> None:
        """Suppressing the live mirror must not blind operators to whether the
        sweep is running at all via other means (log-based metrics/audit)."""
        _mock_dp, _mock_pd, _mock_slack, mock_log = _run("DP_FLEET_MONITOR_RUN_STARTED", {})
        sent_calls = [c for c in mock_log.call_args_list if c.args and c.args[0] == "ALERT_SENT"]
        assert len(sent_calls) == 1
        assert sent_calls[0].kwargs["details"]["mirrored_live"] is False

    def test_run_failed_still_pages_unaffected(self) -> None:
        """The real incident event (DP-WATCHER-003, mirror_live defaults True)
        must be completely unaffected by this suppression. Note: a CRITICAL DP
        event already logs ALERT_SENT a second time via the separate pre-existing
        incident-path plumbing (unrelated to this fix) — only the router.py
        call site this fix touches is asserted on here."""
        mock_dp, mock_pd, _mock_slack, mock_log = _run("DP_FLEET_MONITOR_RUN_FAILED", {})
        mock_dp.assert_called_once()
        mock_pd.assert_called()
        sent_calls = [c for c in mock_log.call_args_list if c.args and c.args[0] == "ALERT_SENT"]
        assert any(c.kwargs["details"].get("mirrored_live") is True for c in sent_calls)
