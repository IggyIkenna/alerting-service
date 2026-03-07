"""Unit tests for the PagerDuty notifier."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alerting_service.notifiers.pagerduty import _PAGERDUTY_ENQUEUE_URL, send_event


@pytest.fixture
def mock_secret_client():
    """Patch get_secret_client to return a fixed routing key."""
    mock_client = MagicMock()
    mock_client.get_secret.return_value = "test-routing-key-abc123"
    with patch(
        "alerting_service.notifiers.pagerduty.get_secret_client", return_value=mock_client
    ) as mock:
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
        with patch(
            "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
        ) as mock_post:
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
        with patch(
            "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
        ) as mock_post:
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
        with patch(
            "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
        ) as mock_post:
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
        with patch(
            "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
        ):
            assert (
                send_event(
                    summary="test", severity="critical", source="alerting-service", details={}
                )
                is True
            )

    def test_severity_error_accepted(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
        ):
            assert (
                send_event(summary="test", severity="error", source="alerting-service", details={})
                is True
            )

    def test_severity_warning_accepted(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
        ):
            assert (
                send_event(
                    summary="test", severity="warning", source="alerting-service", details={}
                )
                is True
            )

    def test_severity_info_accepted(
        self, mock_secret_client: MagicMock, mock_config: MagicMock, mock_log_event: MagicMock
    ) -> None:
        with patch(
            "alerting_service.notifiers.pagerduty.httpx.post", return_value=_make_response(202)
        ):
            assert (
                send_event(summary="test", severity="info", source="alerting-service", details={})
                is True
            )
