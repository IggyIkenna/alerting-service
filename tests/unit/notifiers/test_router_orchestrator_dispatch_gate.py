"""Router tests: the escalation-tier gate for the relocated orchestrator
fast-spawn dispatch (Phase 2 of
alerting_service_escalation_ladder_centralization_2026_08_18.md).

Asserts end-to-end via ``route_event`` (every external sink mocked, mirrors
test_router_dp_mirror_live.py's pattern) that ``dispatch_to_orchestrator``
fires ONLY for a FILE_ISSUE/PAGE_OPERATOR-tier DP_* finding, is gated by the
GCS dispatch-dedup checkpoint for tuple-shaped findings, and stays silent for
every other tier / event family (AUTO_RECOVER, no escalation_tier at all,
deployment lifecycle events).
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


def _run(event_name: str, details: dict[str, object], *, dedup_return: dict[str, object] | None = None):
    with (
        patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=_make_cfg()),
        patch("alerting_service.notifiers.router.get_paging_credentials", return_value={}),
        patch("alerting_service.notifiers.router._persist_config_snapshot"),
        patch("alerting_service.notifiers.router._persist_delivery_record"),
        patch("alerting_service.notifiers.router._publish_kill_switch_event"),
        patch("alerting_service.notifiers.router.send_data_pipeline_alert", return_value=True),
        patch("alerting_service.notifiers.router.pd_send_event", return_value=True),
        patch("alerting_service.notifiers.router.send_uts_live_alert"),
        patch("alerting_service.notifiers.router.log_event"),
        patch(
            "alerting_service.notifiers.orchestrator_dispatch_gate.dispatch_to_orchestrator",
            return_value={"dispatched": True},
        ) as mock_dispatch,
        patch(
            "alerting_service.notifiers.orchestrator_dispatch_gate.check_dispatch_dedup_gcs", return_value=dedup_return
        ) as mock_dedup,
    ):
        route_event(event_name, details)
        return mock_dispatch, mock_dedup


class TestEscalationTierGate:
    def test_file_issue_tier_dispatches(self) -> None:
        mock_dispatch, _mock_dedup = _run(
            "DP_VM_STALL", {"escalation_tier": "file_issue", "vm_name": "cefi-aster-2023-x"}
        )
        mock_dispatch.assert_called_once()

    def test_page_operator_tier_dispatches(self) -> None:
        mock_dispatch, _mock_dedup = _run(
            "DP_VM_STALL", {"escalation_tier": "page_operator", "vm_name": "cefi-aster-2023-x"}
        )
        mock_dispatch.assert_called_once()

    def test_auto_recover_tier_does_not_dispatch(self) -> None:
        mock_dispatch, _mock_dedup = _run("CONSOLIDATOR_DOWN", {"escalation_tier": "auto_recover"})
        mock_dispatch.assert_not_called()

    def test_no_escalation_tier_does_not_dispatch(self) -> None:
        """A non-escalation DP_* event (or a finding missing the field
        entirely) must never fire the fast-spawn dispatch."""
        mock_dispatch, _mock_dedup = _run("DP_VM_PREEMPTED", {})
        mock_dispatch.assert_not_called()

    def test_deployment_lifecycle_event_does_not_dispatch(self) -> None:
        """DEPLOYMENT_* events route through the SAME _route_data_pipeline_event
        function but never carry escalation_tier -- must stay silent."""
        mock_dispatch, _mock_dedup = _run("DEPLOYMENT_FAILED", {"vm_name": "x"})
        mock_dispatch.assert_not_called()


class TestDedupGating:
    def test_tuple_shaped_finding_consults_dedup_before_dispatching(self) -> None:
        mock_dispatch, mock_dedup = _run(
            "DP_RUN_MOSTLY_EMPTY",
            {
                "escalation_tier": "file_issue",
                "asset_group_name": "cefi",
                "data_type": "book_snapshot_5",
                "registry_id": "DP-FETCH-009",
                "max_attempted_at": "2026-08-18T00:00:00Z",
            },
            dedup_return={"skipped": False, "reason": "no_prior_checkpoint_bootstrap"},
        )
        mock_dedup.assert_called_once()
        mock_dispatch.assert_called_once()

    def test_dedup_skip_suppresses_the_dispatch(self) -> None:
        mock_dispatch, mock_dedup = _run(
            "DP_RUN_MOSTLY_EMPTY",
            {
                "escalation_tier": "file_issue",
                "asset_group_name": "cefi",
                "data_type": "book_snapshot_5",
                "registry_id": "DP-FETCH-009",
                "max_attempted_at": "2026-08-18T00:00:00Z",
            },
            dedup_return={"skipped": True, "reason": "no_new_activity_since_checkpoint"},
        )
        mock_dedup.assert_called_once()
        mock_dispatch.assert_not_called()

    def test_vm_only_finding_never_consults_dedup(self) -> None:
        """No (asset_group_name, data_type) tuple -> the tuple-shaped GCS
        dedup is not applicable; the finding still dispatches (bounded only
        by dispatch_to_orchestrator's own relaunch budget, not this gate)."""
        mock_dispatch, mock_dedup = _run(
            "DP_VM_STALL", {"escalation_tier": "file_issue", "vm_name": "cefi-aster-2023-x"}
        )
        mock_dedup.assert_not_called()
        mock_dispatch.assert_called_once()


class TestNeverRaises:
    def test_dispatch_gate_exception_does_not_break_the_alert_route(self) -> None:
        """A gating-logic failure (e.g. the dedup check itself raising,
        despite its own internal never-raises contract) must not prevent the
        rest of route_event from completing."""
        with (
            patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=_make_cfg()),
            patch("alerting_service.notifiers.router.get_paging_credentials", return_value={}),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._publish_kill_switch_event"),
            patch("alerting_service.notifiers.router.send_data_pipeline_alert", return_value=True) as mock_dp,
            patch("alerting_service.notifiers.router.pd_send_event", return_value=True),
            patch("alerting_service.notifiers.router.send_uts_live_alert"),
            patch("alerting_service.notifiers.router.log_event"),
            patch(
                "alerting_service.notifiers.orchestrator_dispatch_gate.dispatch_to_orchestrator",
                side_effect=RuntimeError("boom"),
            ),
        ):
            # Must not raise.
            route_event("DP_VM_STALL", {"escalation_tier": "file_issue", "vm_name": "x"})
        mock_dp.assert_called_once()
