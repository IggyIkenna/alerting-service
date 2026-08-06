"""Unit tests for the PagerDuty notifier."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alerting_service.notifiers.pagerduty import _PAGERDUTY_ENQUEUE_URL, _probe_routing_key, is_available, send_event


@pytest.fixture(autouse=True)
def _clear_routing_key_probe_cache():
    """Clear the process-lifetime probe cache between tests (it is an
    ``lru_cache``, so without this every test after the first would reuse
    whatever the FIRST test's mocked Secret Manager returned for the same
    project_id, regardless of that test's own fixtures)."""
    _probe_routing_key.cache_clear()
    yield
    _probe_routing_key.cache_clear()


@pytest.fixture
def mock_secret_client():
    """Patch get_secret_client to return a fixed routing key."""
    mock_client = MagicMock()
    mock_client.get_secret.return_value = "test-routing-key-abc123"
    with patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=mock_client) as mock:
        yield mock


@pytest.fixture
def mock_config():
    """Patch UnifiedCloudConfig to provide a fixed project_id."""
    mock_cfg = MagicMock()
    mock_cfg.gcp_project_id = "test-project"
    with patch("alerting_service.notifiers.pagerduty.UnifiedCloudConfig", return_value=mock_cfg):
        yield mock_cfg


@pytest.fixture
def mock_log_event():
    """Suppress log_event calls."""
    with patch("alerting_service.notifiers.pagerduty.log_event") as mock:
        yield mock


def _make_response(status_code: int, text: str = "") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    return resp


class TestSendEvent:
    def test_returns_true_on_202(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch("alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)) as mock_post:
            result = send_event(
                summary="Kill switch fired",
                severity="critical",
                source="execution-service",
                details={"strategy": "s1"},
            )

        assert result is True
        mock_post.assert_called_once()

    def test_posts_to_correct_url(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch("alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)) as mock_post:
            send_event(
                summary="test",
                severity="info",
                source="alerting-service",
                details={},
            )

        call_url = mock_post.call_args[0][0]
        assert call_url == _PAGERDUTY_ENQUEUE_URL

    def test_payload_contains_routing_key_and_summary(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch("alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)) as mock_post:
            send_event(
                summary="Circuit breaker opened",
                severity="critical",
                source="execution-service",
                details={"venue": "binance"},
            )

        json_payload: dict[str, object] = mock_post.call_args.kwargs["json"]
        assert json_payload["routing_key"] == "test-routing-key-abc123"
        assert json_payload["event_action"] == "trigger"
        inner = json_payload["payload"]
        assert isinstance(inner, dict)
        assert inner["summary"] == "Circuit breaker opened"
        assert inner["severity"] == "critical"
        assert inner["source"] == "execution-service"
        assert inner["custom_details"] == {"venue": "binance"}

    def test_returns_false_on_non_202(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.pagerduty.httpx.post",
            return_value=_make_response(400, "bad request"),
        ):
            result = send_event(
                summary="test",
                severity="warning",
                source="alerting-service",
                details={},
            )

        assert result is False

    def test_returns_false_on_http_error_and_does_not_raise(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.pagerduty.httpx.post",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            result = send_event(
                summary="test",
                severity="error",
                source="alerting-service",
                details={},
            )

        assert result is False

    def test_severity_critical_accepted(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch("alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)):
            assert send_event(summary="test", severity="critical", source="alerting-service", details={}) is True

    def test_severity_error_accepted(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch("alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)):
            assert send_event(summary="test", severity="error", source="alerting-service", details={}) is True

    def test_severity_warning_accepted(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch("alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)):
            assert send_event(summary="test", severity="warning", source="alerting-service", details={}) is True

    def test_severity_info_accepted(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch("alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)):
            assert send_event(summary="test", severity="info", source="alerting-service", details={}) is True


# ── 2026-08-06: capability-probe pattern — missing secret degrades, never raises ──
class TestCapabilityProbe:
    """PagerDuty was never provisioned (``alerting-pagerduty-routing-key`` does
    not exist in Secret Manager) — the routing-key lookup used to RAISE
    ``RuntimeError`` unguarded inside ``send_event``, crashing every caller.
    These tests pin the fix: a missing/failing secret degrades to
    ``send_event() -> False`` / ``is_available() -> False``, never raises."""

    def test_missing_secret_send_event_returns_false_not_raises(self, mock_config: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.get_secret.return_value = None
        with patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=mock_client):
            result = send_event(summary="x", severity="critical", source="s", details={})
        assert result is False

    def test_secret_manager_exception_send_event_returns_false_not_raises(self, mock_config: MagicMock) -> None:
        with patch(
            "alerting_service.notifiers.pagerduty.get_secret_client",
            side_effect=RuntimeError("permission denied"),
        ):
            result = send_event(summary="x", severity="critical", source="s", details={})
        assert result is False

    def test_missing_secret_never_posts_to_pagerduty(self, mock_config: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.get_secret.return_value = None
        with (
            patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=mock_client),
            patch("alerting_service.notifiers.pagerduty.httpx.post") as mock_post,
        ):
            send_event(summary="x", severity="critical", source="s", details={})
        mock_post.assert_not_called()

    def test_is_available_false_when_secret_missing(self, mock_config: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.get_secret.return_value = None
        with patch("alerting_service.notifiers.pagerduty.get_secret_client", return_value=mock_client):
            assert is_available("test-project") is False

    def test_is_available_true_when_secret_present(self, mock_secret_client: MagicMock, mock_config: MagicMock) -> None:
        assert is_available("test-project") is True

    def test_secret_manager_probed_only_once_across_repeated_calls(self, mock_config: MagicMock) -> None:
        """The probe is cached (lru_cache) — a missing secret is only logged/
        probed ONCE per process, not once per alert fire (the original bug:
        every recurring alert independently re-hit the crash)."""
        mock_client = MagicMock()
        mock_client.get_secret.return_value = None
        with patch(
            "alerting_service.notifiers.pagerduty.get_secret_client", return_value=mock_client
        ) as mock_get_client:
            send_event(summary="a", severity="critical", source="s", details={})
            send_event(summary="b", severity="critical", source="s", details={})
            send_event(summary="c", severity="critical", source="s", details={})
        mock_get_client.assert_called_once()

    def test_present_secret_still_used_for_the_post(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch("alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)) as mock_post:
            send_event(summary="x", severity="critical", source="s", details={})
        assert mock_post.call_args.kwargs["json"]["routing_key"] == "test-routing-key-abc123"
