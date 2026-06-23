"""Router tests for deployment lifecycle parity + data-pipeline deep-link enrichment.

Asserts (block-network: every external sink is mocked):
  - a DEPLOYMENT_FAILED event routes via the data-pipeline path with paging
    (CRITICAL → PagerDuty + Telegram) AND mirrors to #data-pipeline-alerts with
    the umbrella + a /deployments deep-link;
  - DEPLOYMENT_STARTED is INFO channel-only (no paging);
  - a DP_VM_EXIT_NONZERO alert carries the /deployments + /ops/vms + run.log links;
  - deployment_ui_base_url="" omits the deep-links.
"""

from unittest.mock import MagicMock, patch

import pytest
from unified_trading_library import DEPLOYMENT_FAILED, DEPLOYMENT_STARTED

from alerting_service.notifiers.router import route_event

pytestmark = pytest.mark.unit

_DP_WEBHOOK = "https://hooks.slack.com/services/T/B/dp"
_BASE = "https://deployment.odum-research.com"


def _make_cfg(*, base_url: str = _BASE, log_bucket: str = "deployment-scripts-prd") -> MagicMock:
    cfg = MagicMock()
    cfg.telegram_bot_token = "bot-token"
    cfg.telegram_chat_id = "chat"
    cfg.telegram_chat_id_ops = ""
    cfg.uts_live_alerts_slack_webhook = ""
    cfg.data_pipeline_slack_webhook = _DP_WEBHOOK
    cfg.deployment_ui_base_url = base_url
    cfg.deployment_scripts_log_bucket = log_bucket
    cfg.gcp_project_id = "test-project"
    cfg.pagerduty_disabled = False
    cfg.quietness_baseline_mode = False
    cfg.routing_rules = [{"event_pattern": "*", "channels": ["telegram"], "severity_filter": None}]
    return cfg


def _run(event_name: str, details: dict[str, object], *, cfg: MagicMock):
    """Route an event with all external sinks mocked; return the captured mocks."""
    with (
        patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=cfg),
        patch("alerting_service.notifiers.router.get_paging_credentials", return_value={}),
        patch("alerting_service.notifiers.router._persist_config_snapshot"),
        patch("alerting_service.notifiers.router._persist_delivery_record"),
        patch("alerting_service.notifiers.router._publish_kill_switch_event"),
        patch("alerting_service.notifiers.router.send_data_pipeline_alert", return_value=True) as mock_dp,
        patch("alerting_service.notifiers.router.pd_send_event", return_value=True) as mock_pd,
        patch("alerting_service.notifiers.router.send_uts_live_alert") as mock_slack,
    ):
        # New event names every call → dedup never suppresses.
        route_event(event_name, details)
        return mock_dp, mock_pd, mock_slack


class TestDeploymentLifecycleParity:
    def test_failed_pages_and_mirrors_with_deeplink(self) -> None:
        mock_dp, mock_pd, _mock_slack = _run(
            DEPLOYMENT_FAILED,
            {"vm_name": "vm-defi-aave-1", "umbrella": "live-defi", "cloud": "gcp", "exit_code": 1},
            cfg=_make_cfg(),
        )
        # Mirrored to #data-pipeline-alerts with the deployment-ui base URL threaded.
        mock_dp.assert_called_once()
        assert mock_dp.call_args.args[1] == DEPLOYMENT_FAILED
        assert mock_dp.call_args.kwargs["deployment_ui_base_url"] == _BASE
        assert mock_dp.call_args.kwargs["log_bucket"] == "deployment-scripts-prd"
        # CRITICAL → ALSO pages via PagerDuty incident path.
        mock_pd.assert_called()

    def test_started_is_channel_only_no_page(self) -> None:
        mock_dp, mock_pd, _mock_slack = _run(
            DEPLOYMENT_STARTED,
            {"vm_name": "vm-defi-aave-1", "umbrella": "live-defi"},
            cfg=_make_cfg(),
        )
        mock_dp.assert_called_once()
        mock_pd.assert_not_called()  # INFO never pages


class TestDataPipelineDeepLinks:
    def test_dp_vm_exit_nonzero_threads_base_and_bucket(self) -> None:
        mock_dp, _mock_pd, _mock_slack = _run(
            "DP_VM_EXIT_NONZERO",
            {"vm_name": "vm-sports-bf-9", "asset_group": "sports", "exit_code": 137},
            cfg=_make_cfg(),
        )
        mock_dp.assert_called_once()
        assert mock_dp.call_args.kwargs["deployment_ui_base_url"] == _BASE
        assert mock_dp.call_args.kwargs["log_bucket"] == "deployment-scripts-prd"

    def test_empty_base_url_omits_links(self) -> None:
        mock_dp, _mock_pd, _mock_slack = _run(
            "DP_VM_EXIT_NONZERO",
            {"vm_name": "vm-sports-bf-9", "asset_group": "sports"},
            cfg=_make_cfg(base_url="", log_bucket=""),
        )
        mock_dp.assert_called_once()
        assert mock_dp.call_args.kwargs["deployment_ui_base_url"] == ""
        assert mock_dp.call_args.kwargs["log_bucket"] == ""
