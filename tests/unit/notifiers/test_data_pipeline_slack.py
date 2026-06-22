"""Unit tests for the data-pipeline-alerts → Slack mirror notifier."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alerting_service.notifiers.data_pipeline_slack import (
    _SLACK_TEXT_LIMIT,
    _build_action_block,
    _build_blocks,
    _build_trace_block,
    _emoji_for,
    send_data_pipeline_alert,
)

pytestmark = pytest.mark.unit

_WEBHOOK = "https://hooks.slack.com/services/T/B/secret"
_BASE = "https://deployment.odum-research.com"


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


class TestTraceBlock:
    def test_evidence_dict_rendered_as_json_code_block(self) -> None:
        block = _build_trace_block(
            {"fetch_evidence": {"http_status": 401, "source": "thegraph", "endpoint": "/subgraphs"}}
        )
        assert block is not None
        text = block["text"]["text"]
        assert "```" in text
        assert "http_status" in text
        assert "401" in text

    def test_error_message_rendered(self) -> None:
        block = _build_trace_block({"error_message": "boom: connection refused"})
        assert block is not None
        assert "boom: connection refused" in block["text"]["text"]

    def test_none_when_no_trace_payload(self) -> None:
        assert _build_trace_block({"asset_group": "defi"}) is None
        assert _build_trace_block({}) is None

    def test_truncated_to_slack_limit(self) -> None:
        huge = "x" * (_SLACK_TEXT_LIMIT * 2)
        block = _build_trace_block({"run_log_tail": huge})
        assert block is not None
        text = block["text"]["text"]
        # The pasted trace portion stays under the Slack section text limit.
        assert len(text) <= _SLACK_TEXT_LIMIT + len("*Trace (run_log_tail):*\n```\n\n```")
        assert "truncated" in text


class TestActionBlock:
    def test_vm_links_present_when_vm_name_and_base(self) -> None:
        block = _build_action_block({"vm_name": "vm-defi-aave-123"}, _BASE, "deployment-scripts-prd")
        assert block is not None
        text = block["elements"][0]["text"]
        assert f"{_BASE}/ops/vms/vm-defi-aave-123|VM logs" in text
        assert f"{_BASE}/deployments/vm-defi-aave-123|Deployment" in text
        assert "deployment-scripts-prd/vm-logs/vm-defi-aave-123" in text  # GCS run.log

    def test_data_status_link_when_asset_group(self) -> None:
        block = _build_action_block({"asset_group": "defi", "service": "mtds"}, _BASE, "")
        assert block is not None
        text = block["elements"][0]["text"]
        assert "/service/mtds/data-status?asset_group=defi|Data status" in text

    def test_omitted_when_base_url_empty(self) -> None:
        # No base URL → no UI deep-links (run.log still omitted without bucket).
        assert _build_action_block({"vm_name": "vm-x", "asset_group": "defi"}, "", "") is None

    def test_omitted_when_inputs_absent(self) -> None:
        assert _build_action_block({}, _BASE, "deployment-scripts-prd") is None

    def test_runlog_link_without_base_url(self) -> None:
        # GCS run.log needs only the bucket + vm_name, not the deployment-ui base.
        block = _build_action_block({"vm_name": "vm-x"}, "", "deployment-scripts-prd")
        assert block is not None
        text = block["elements"][0]["text"]
        assert "deployment-scripts-prd/vm-logs/vm-x" in text
        assert "/ops/vms/" not in text  # no UI link without base


class TestEnrichedBlocks:
    def test_dp_vm_exit_nonzero_has_all_deeplinks(self) -> None:
        blocks = _build_blocks(
            "DP_VM_EXIT_NONZERO",
            "[DP_VM_EXIT_NONZERO] backfill VM crashed",
            {"severity": "CRITICAL", "vm_name": "vm-sports-bf-9", "exit_code": 137, "asset_group": "sports"},
            deployment_ui_base_url=_BASE,
            log_bucket="deployment-scripts-prd",
        )
        flat = str(blocks)
        assert "/deployments/vm-sports-bf-9" in flat
        assert "/ops/vms/vm-sports-bf-9" in flat
        assert "vm-logs/vm-sports-bf-9" in flat  # run.log
        assert "exit_code" not in flat  # rendered as label, not raw key
        assert "137" in flat

    def test_no_extra_blocks_without_base_or_trace(self) -> None:
        blocks = _build_blocks("DP_VM_STALL", "x", {"asset_group": "defi"})
        # header + summary + fields only.
        assert len(blocks) == 3


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
