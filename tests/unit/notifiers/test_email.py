"""Unit tests for the CRITICAL-severity email fallback notifier."""

from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from alerting_service.notifiers.email import _send_smtp, send_critical_fallback


@pytest.fixture
def mock_config() -> MagicMock:
    cfg = MagicMock()
    cfg.email_smtp_host = "smtp.example.com"
    cfg.email_smtp_port = 587
    cfg.email_smtp_username = "cfg-user"
    cfg.email_smtp_password = "cfg-pass"
    cfg.email_from_address = "alerts@example.com"
    cfg.email_to = ["ops@example.com", "oncall@example.com"]
    return cfg


@pytest.fixture
def empty_sm_creds():
    """No SM-hot-reloaded email creds -> config.py fields are authoritative."""
    with patch("alerting_service.notifiers.email.get_paging_credentials", return_value={}) as mock:
        yield mock


@pytest.fixture
def mock_log_event():
    with patch("alerting_service.notifiers.email.log_event") as mock:
        yield mock


class TestSendSmtp:
    def test_returns_false_when_host_empty(self, mock_log_event: MagicMock) -> None:
        result = _send_smtp(
            smtp_host="",
            smtp_port=587,
            smtp_username="u",
            smtp_password="p",
            from_address="a@b.com",
            to_addresses=["ops@example.com"],
            subject="subj",
            body="body",
        )
        assert result is False

    def test_returns_false_when_no_recipients(self, mock_log_event: MagicMock) -> None:
        result = _send_smtp(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="u",
            smtp_password="p",
            from_address="a@b.com",
            to_addresses=[],
            subject="subj",
            body="body",
        )
        assert result is False

    def test_returns_true_and_logs_in_on_success(self, mock_log_event: MagicMock) -> None:
        mock_server = MagicMock()
        mock_smtp_cls = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server
        with patch("alerting_service.notifiers.email.smtplib.SMTP", mock_smtp_cls):
            result = _send_smtp(
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="u",
                smtp_password="p",
                from_address="a@b.com",
                to_addresses=["ops@example.com"],
                subject="subj",
                body="body",
            )
        assert result is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("u", "p")
        mock_server.send_message.assert_called_once()
        mock_log_event.assert_called_once()
        assert mock_log_event.call_args.args[0] == "EMAIL_FALLBACK_SENT"

    def test_skips_login_when_no_credentials(self, mock_log_event: MagicMock) -> None:
        mock_server = MagicMock()
        mock_smtp_cls = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server
        with patch("alerting_service.notifiers.email.smtplib.SMTP", mock_smtp_cls):
            _send_smtp(
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="",
                smtp_password="",
                from_address="a@b.com",
                to_addresses=["ops@example.com"],
                subject="subj",
                body="body",
            )
        mock_server.login.assert_not_called()

    def test_returns_false_and_logs_on_smtp_exception(self, mock_log_event: MagicMock) -> None:
        with patch(
            "alerting_service.notifiers.email.smtplib.SMTP",
            side_effect=smtplib.SMTPConnectError(421, "cannot connect"),
        ):
            result = _send_smtp(
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="u",
                smtp_password="p",
                from_address="a@b.com",
                to_addresses=["ops@example.com"],
                subject="subj",
                body="body",
            )
        assert result is False
        mock_log_event.assert_called_once()
        assert mock_log_event.call_args.args[0] == "EMAIL_FALLBACK_FAILED"

    def test_returns_false_on_os_error_never_raises(self, mock_log_event: MagicMock) -> None:
        with patch("alerting_service.notifiers.email.smtplib.SMTP", side_effect=OSError("network unreachable")):
            result = _send_smtp(
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="u",
                smtp_password="p",
                from_address="a@b.com",
                to_addresses=["ops@example.com"],
                subject="subj",
                body="body",
            )
        assert result is False


class TestSendCriticalFallback:
    def test_uses_config_when_sm_creds_empty(
        self, mock_config: MagicMock, empty_sm_creds: MagicMock, mock_log_event: MagicMock
    ) -> None:
        mock_server = MagicMock()
        mock_smtp_cls = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server
        with patch("alerting_service.notifiers.email.smtplib.SMTP", mock_smtp_cls):
            result = send_critical_fallback("bucket down", "consolidator-rules", {"bucket": "b1"}, mock_config)
        assert result is True
        mock_server.login.assert_called_once_with("cfg-user", "cfg-pass")

    def test_sm_creds_take_precedence_over_config(self, mock_config: MagicMock, mock_log_event: MagicMock) -> None:
        mock_server = MagicMock()
        mock_smtp_cls = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server
        with (
            patch(
                "alerting_service.notifiers.email.get_paging_credentials",
                return_value={
                    "email_smtp_username": "sm-user",
                    "email_smtp_password": "sm-pass",
                    "email_from_address": "sm-from@example.com",
                },
            ),
            patch("alerting_service.notifiers.email.smtplib.SMTP", mock_smtp_cls),
        ):
            send_critical_fallback("bucket down", "consolidator-rules", {"bucket": "b1"}, mock_config)
        mock_server.login.assert_called_once_with("sm-user", "sm-pass")

    def test_subject_carries_critical_prefix_and_summary(
        self, mock_config: MagicMock, empty_sm_creds: MagicMock, mock_log_event: MagicMock
    ) -> None:
        mock_server = MagicMock()
        mock_smtp_cls = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server
        with patch("alerting_service.notifiers.email.smtplib.SMTP", mock_smtp_cls):
            send_critical_fallback("manifest consolidator DOWN", "consolidator-rules", {}, mock_config)
        sent_message = mock_server.send_message.call_args.args[0]
        assert sent_message["Subject"] == "[CRITICAL] manifest consolidator DOWN"

    def test_recipients_come_from_config_email_to(
        self, mock_config: MagicMock, empty_sm_creds: MagicMock, mock_log_event: MagicMock
    ) -> None:
        mock_server = MagicMock()
        mock_smtp_cls = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server
        with patch("alerting_service.notifiers.email.smtplib.SMTP", mock_smtp_cls):
            send_critical_fallback("x", "s", {}, mock_config)
        sent_message = mock_server.send_message.call_args.args[0]
        assert sent_message["To"] == "ops@example.com, oncall@example.com"

    def test_returns_false_when_no_recipients_configured(
        self, mock_config: MagicMock, empty_sm_creds: MagicMock, mock_log_event: MagicMock
    ) -> None:
        mock_config.email_to = []
        result = send_critical_fallback("x", "s", {}, mock_config)
        assert result is False
