"""Unit tests for the relocated GitHub repository_dispatch notifier
(orchestrator_dispatch.py, Phase 2 of
alerting_service_escalation_ladder_centralization_2026_08_18.md).

Mirrors test_pagerduty.py's capability-probe test conventions (cache-clear
fixture, mocked get_secret_client/UnifiedCloudConfig/httpx-equivalent) and
deployment-service's test_escalation_dedup.py coverage for the relaunch-
budget-gated context text (DO NOT RELAUNCH vs RELAUNCH).
"""

from unittest.mock import MagicMock, patch

import pytest

from alerting_service.notifiers.orchestrator_dispatch import (
    _DISPATCH_PM_REPO,
    _get_cloud_config,
    _probe_gh_pat,
    _target_repo_for,
    dispatch_to_orchestrator,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear the process-lifetime lru_caches between tests -- without this,
    every test after the first reuses whatever the FIRST test's mocked
    UnifiedCloudConfig/Secret Manager returned (mirrors test_pagerduty.py's
    _clear_routing_key_probe_cache fixture)."""
    _probe_gh_pat.cache_clear()
    _get_cloud_config.cache_clear()
    yield
    _probe_gh_pat.cache_clear()
    _get_cloud_config.cache_clear()


@pytest.fixture
def mock_config():
    mock_cfg = MagicMock()
    mock_cfg.gcp_project_id = "test-project"
    with patch("alerting_service.notifiers.orchestrator_dispatch.UnifiedCloudConfig", return_value=mock_cfg):
        yield mock_cfg


@pytest.fixture
def mock_secret_client():
    mock_client = MagicMock()
    mock_client.get_secret.return_value = "gh-pat-abc123"
    with patch("alerting_service.notifiers.orchestrator_dispatch.get_secret_client", return_value=mock_client) as mock:
        yield mock


@pytest.fixture
def mock_log_event():
    with patch("alerting_service.notifiers.orchestrator_dispatch.log_event") as mock:
        yield mock


_UNBOUNDED_BUDGET: dict[str, object] = {
    "bounded": False,
    "vm_prefix": "cefi-aster-",
    "shard_key": "cefi-aster-|2023",
    "dispatches_today": 1,
    "max_per_day": 2,
}


@pytest.fixture(autouse=True)
def _no_relaunch_budget():
    """Default: budget check reports unbounded (or is not consulted for
    non-VM-lifecycle events) so tests focus on the dispatch call itself,
    unless a test overrides this."""
    with patch(
        "alerting_service.notifiers.orchestrator_dispatch.check_relaunch_dispatch_budget",
        return_value=_UNBOUNDED_BUDGET,
    ) as mock:
        yield mock


def _make_response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.close = MagicMock()
    return resp


class TestTargetRepoFor:
    def test_vm_lifecycle_event_targets_deployment_service(self) -> None:
        assert _target_repo_for("DP_VM_STALL") == "deployment-service"
        assert _target_repo_for("DP_VM_EXIT_NONZERO") == "deployment-service"
        assert _target_repo_for("CONSOLIDATOR_DOWN") == "deployment-service"

    def test_non_vm_lifecycle_event_targets_mtds(self) -> None:
        assert _target_repo_for("DP_RUN_MOSTLY_EMPTY") == "market-tick-data-service"
        assert _target_repo_for("DP_UNPROVEN_HONEST_ABSENCE") == "market-tick-data-service"


class TestDispatchSuccessPath:
    def test_returns_dispatched_true_on_2xx(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen",
            return_value=_make_response(204),
        ) as mock_urlopen:
            result = dispatch_to_orchestrator(
                "DP_RUN_MOSTLY_EMPTY",
                "summary text",
                {"registry_id": "DP-FETCH-009", "severity": "CRITICAL"},
            )
        assert result == {"dispatched": True, "reason": "http_204", "http_status": 204}
        mock_urlopen.assert_called_once()

    def test_posts_to_the_pm_repo_dispatches_endpoint(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen",
            return_value=_make_response(204),
        ) as mock_urlopen:
            dispatch_to_orchestrator("DP_RUN_MOSTLY_EMPTY", "summary", {})
        request = mock_urlopen.call_args[0][0]
        assert request.full_url == f"https://api.github.com/repos/{_DISPATCH_PM_REPO}/dispatches"

    def test_payload_shape_matches_the_deployment_service_original(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        import json

        with patch(
            "alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen",
            return_value=_make_response(204),
        ) as mock_urlopen:
            dispatch_to_orchestrator(
                "DP_RUN_MOSTLY_EMPTY",
                "flat capture",
                {"registry_id": "DP-FETCH-009", "severity": "CRITICAL"},
            )
        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        assert body["event_type"] == "escalate-to-orchestrator"
        payload = body["client_payload"]
        assert payload["repo"] == "market-tick-data-service"
        assert payload["pr_number"] == "0"
        assert payload["wall_type"] == "data_pipeline_failure"
        assert payload["authoring_slot"] == "dp-fleet-monitor"
        assert payload["model"] == "sonnet"
        assert "DP-FETCH-009" in payload["context"]
        assert "flat capture" in payload["context"]

    def test_logs_orchestrator_dispatch_sent_on_success(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen",
            return_value=_make_response(204),
        ):
            dispatch_to_orchestrator("DP_RUN_MOSTLY_EMPTY", "summary", {"registry_id": "DP-FETCH-009"})
        mock_log_event.assert_called_once()
        assert mock_log_event.call_args[0][0] == "ORCHESTRATOR_DISPATCH_SENT"

    def test_does_not_log_on_non_2xx(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen",
            return_value=_make_response(422),
        ):
            result = dispatch_to_orchestrator("DP_RUN_MOSTLY_EMPTY", "summary", {})
        assert result["dispatched"] is False
        mock_log_event.assert_not_called()


class TestDispatchFailurePaths:
    def test_no_gh_token_returns_no_gh_token_reason_and_never_posts(self, mock_config: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.get_secret.return_value = None
        with (
            patch("alerting_service.notifiers.orchestrator_dispatch.get_secret_client", return_value=mock_client),
            patch("alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen") as mock_urlopen,
        ):
            result = dispatch_to_orchestrator("DP_RUN_MOSTLY_EMPTY", "summary", {})
        assert result == {"dispatched": False, "reason": "no_gh_token"}
        mock_urlopen.assert_not_called()

    def test_secret_manager_exception_returns_false_not_raises(self, mock_config: MagicMock) -> None:
        with patch(
            "alerting_service.notifiers.orchestrator_dispatch.get_secret_client",
            side_effect=RuntimeError("permission denied"),
        ):
            result = dispatch_to_orchestrator("DP_RUN_MOSTLY_EMPTY", "summary", {})
        assert result == {"dispatched": False, "reason": "no_gh_token"}

    def test_http_error_returns_false_with_status_and_does_not_raise(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        import urllib.error

        with patch(
            "alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(url="x", code=422, msg="unprocessable", hdrs=None, fp=None),  # type: ignore[arg-type]
        ):
            result = dispatch_to_orchestrator("DP_RUN_MOSTLY_EMPTY", "summary", {})
        assert result == {"dispatched": False, "reason": "http_422", "http_status": 422}

    def test_network_failure_returns_false_and_does_not_raise(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen",
            side_effect=TimeoutError("connection timed out"),
        ):
            result = dispatch_to_orchestrator("DP_RUN_MOSTLY_EMPTY", "summary", {})
        assert result["dispatched"] is False
        assert result["reason"].startswith("error:")


class TestGhPatCapabilityProbe:
    def test_gh_pat_probed_only_once_across_repeated_calls(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        """Deviation from the deployment-service original: cached for the
        process lifetime (alerting-service is a warm Cloud Run SERVICE, not
        a fresh-container-per-execution Cloud Run JOB) -- see the module
        docstring."""
        with patch(
            "alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen",
            return_value=_make_response(204),
        ):
            dispatch_to_orchestrator("DP_RUN_MOSTLY_EMPTY", "a", {})
            dispatch_to_orchestrator("DP_RUN_MOSTLY_EMPTY", "b", {})
            dispatch_to_orchestrator("DP_RUN_MOSTLY_EMPTY", "c", {})
        mock_secret_client.assert_called_once()


class TestRelaunchContextText:
    def test_vm_lifecycle_finding_gets_relaunch_instruction_when_unbounded(
        self,
        mock_secret_client: MagicMock,
        mock_config: MagicMock,
        mock_log_event: MagicMock,
        _no_relaunch_budget: MagicMock,
    ) -> None:
        import json

        with patch(
            "alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen",
            return_value=_make_response(204),
        ) as mock_urlopen:
            dispatch_to_orchestrator(
                "DP_VM_STALL",
                "vm stalled",
                {
                    "vm_name": "cefi-aster-2023-20260818-030139",
                    "relaunch_launcher": "launch-cefi-sharded-backfill.sh",
                    "deployment_id": "d-1",
                    "asset_group": "cefi",
                },
            )
        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        context = body["client_payload"]["context"]
        assert "RELAUNCH vm=cefi-aster-2023-20260818-030139" in context
        assert "DO NOT RELAUNCH" not in context
        assert body["client_payload"]["repo"] == "deployment-service"

    def test_vm_lifecycle_finding_gets_do_not_relaunch_when_bounded(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        import json

        with (
            patch(
                "alerting_service.notifiers.orchestrator_dispatch.check_relaunch_dispatch_budget",
                return_value={
                    "bounded": True,
                    "vm_prefix": "cefi-aster-",
                    "shard_key": "cefi-aster-|2023",
                    "dispatches_today": 2,
                    "max_per_day": 2,
                },
            ),
            patch(
                "alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen",
                return_value=_make_response(204),
            ) as mock_urlopen,
        ):
            dispatch_to_orchestrator(
                "DP_VM_STALL",
                "vm stalled",
                {"vm_name": "cefi-aster-2023-20260818-030139"},
            )
        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        context = body["client_payload"]["context"]
        assert "DO NOT RELAUNCH vm=cefi-aster-2023-20260818-030139" in context
        assert "cefi-aster-|2023" in context

    def test_non_vm_lifecycle_finding_never_gets_relaunch_text(
        self,
        mock_secret_client: MagicMock,
        mock_config: MagicMock,
        mock_log_event: MagicMock,
        _no_relaunch_budget: MagicMock,
    ) -> None:
        import json

        with patch(
            "alerting_service.notifiers.orchestrator_dispatch.urllib.request.urlopen",
            return_value=_make_response(204),
        ) as mock_urlopen:
            dispatch_to_orchestrator("DP_RUN_MOSTLY_EMPTY", "flat run", {"asset_group": "cefi"})
        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        context = body["client_payload"]["context"]
        assert "RELAUNCH" not in context
        _no_relaunch_budget.assert_not_called()
