"""Unit tests for _PagingCredentialsReloader SM hot-reload (Phase 4 / plan line 346).

Covers:
  1. Initial state — empty before start()
  2. start(None) — mock mode: no SM call, stays empty, no background thread
  3. start(project_id) with SM returning credentials — bot_token / chat_id / chat_id_ops populated
  4. SM fetch failure on _refresh — keeps previous credentials (no regression)
  5. stop() — joins thread, idempotent
  6. Double start() — idempotent, no duplicate thread
  7. get_paging_credentials() module function — returns dict with all 3 keys
  8. Credential refresh on SM change — second SM response supersedes first
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from alerting_service.config_reloaders import (
    _PagingCredentialsReloader,
    get_paging_credentials,
)

pytestmark = pytest.mark.unit

_SM_BOT_TOKEN = "alerting-telegram-bot-token"
_SM_CHAT_ID = "alerting-telegram-chat-id"
_SM_CHAT_ID_OPS = "alerting-telegram-chat-id-ops"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sm_mock(bot: str = "bot-abc", chat: str = "chat-123", ops: str = "ops-456") -> MagicMock:
    """Return a SecretManagerClient mock that returns the given credential strings."""
    sm = MagicMock()
    sm.get_secrets.return_value = {
        _SM_BOT_TOKEN: bot,
        _SM_CHAT_ID: chat,
        _SM_CHAT_ID_OPS: ops,
    }
    return sm


# ---------------------------------------------------------------------------
# 1. Initial state — empty before start()
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_getters_return_empty_before_start(self) -> None:
        r = _PagingCredentialsReloader()
        assert r.get_bot_token() == ""
        assert r.get_chat_id() == ""
        assert r.get_chat_id_ops() == ""

    def test_not_started_flag(self) -> None:
        r = _PagingCredentialsReloader()
        assert not r._started


# ---------------------------------------------------------------------------
# 2. start(None) — mock mode, no SM call
# ---------------------------------------------------------------------------


class TestStartWithoutProjectId:
    def test_no_sm_call_when_project_id_is_none(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=0.01)
        with patch("alerting_service.config_reloaders.SecretManagerClient") as mock_sm_cls:
            r.start(project_id=None)
            mock_sm_cls.assert_not_called()
        assert r.get_bot_token() == ""
        assert r.get_chat_id() == ""

    def test_no_background_thread_when_no_project_id(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=0.01)
        with patch("alerting_service.config_reloaders.SecretManagerClient"):
            r.start(project_id=None)
        assert r._thread is None


# ---------------------------------------------------------------------------
# 3. start(project_id) with SM returning credentials
# ---------------------------------------------------------------------------


class TestStartWithCredentials:
    def test_credentials_loaded_from_sm_on_start(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        sm_mock = _make_sm_mock(bot="tok-xyz", chat="cid-111", ops="ops-222")
        with patch("alerting_service.config_reloaders.SecretManagerClient", return_value=sm_mock):
            r.start(project_id="test-project")
        try:
            assert r.get_bot_token() == "tok-xyz"
            assert r.get_chat_id() == "cid-111"
            assert r.get_chat_id_ops() == "ops-222"
        finally:
            r.stop()

    def test_background_thread_started_when_credentials_loaded(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        sm_mock = _make_sm_mock()
        with patch("alerting_service.config_reloaders.SecretManagerClient", return_value=sm_mock):
            r.start(project_id="test-project")
        try:
            assert r._thread is not None
            assert r._thread.is_alive()
        finally:
            r.stop()

    def test_partial_sm_response_only_sets_present_keys(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        sm = MagicMock()
        sm.get_secrets.return_value = {_SM_BOT_TOKEN: "tok-only"}
        with patch("alerting_service.config_reloaders.SecretManagerClient", return_value=sm):
            r.start(project_id="test-project")
        try:
            assert r.get_bot_token() == "tok-only"
            assert r.get_chat_id() == ""
            assert r.get_chat_id_ops() == ""
        finally:
            r.stop()


# ---------------------------------------------------------------------------
# 4. SM fetch failure — keeps previous credentials
# ---------------------------------------------------------------------------


class TestSmFetchFailure:
    def test_refresh_failure_keeps_old_credentials(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        sm_mock = _make_sm_mock(bot="tok-stable")
        with patch("alerting_service.config_reloaders.SecretManagerClient", return_value=sm_mock):
            r.start(project_id="test-project")
            # Simulate a refresh failure while SM patch is still active
            sm_mock.get_secrets.side_effect = RuntimeError("SM unavailable")
            with patch("alerting_service.config_reloaders.log_event"):
                r._refresh()
            assert r.get_bot_token() == "tok-stable", "Old credentials must survive refresh failure"
        r.stop()

    def test_initial_sm_error_leaves_empty(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        sm = MagicMock()
        sm.get_secrets.side_effect = RuntimeError("SM down")
        with patch("alerting_service.config_reloaders.SecretManagerClient", return_value=sm):
            r.start(project_id="test-project")
        assert r.get_bot_token() == ""
        assert r._thread is None


# ---------------------------------------------------------------------------
# 5. stop() — joins thread, idempotent
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_terminates_background_thread(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        sm_mock = _make_sm_mock()
        with patch("alerting_service.config_reloaders.SecretManagerClient", return_value=sm_mock):
            r.start(project_id="test-project")
        assert r._thread is not None and r._thread.is_alive()
        r.stop()
        assert not r._started

    def test_stop_is_idempotent(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        with patch("alerting_service.config_reloaders.SecretManagerClient", return_value=_make_sm_mock()):
            r.start(project_id="test-project")
        r.stop()
        r.stop()  # second stop must not raise


# ---------------------------------------------------------------------------
# 6. Double start() — idempotent
# ---------------------------------------------------------------------------


class TestDoubleStart:
    def test_second_start_is_noop(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        call_count = 0

        def counting_sm(*_args: object, **_kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            return _make_sm_mock()

        with patch("alerting_service.config_reloaders.SecretManagerClient", side_effect=counting_sm):
            r.start(project_id="test-project")
            r.start(project_id="test-project")  # second call must be a no-op
        try:
            assert call_count == 1, "SM should only be called once even if start() called twice"
        finally:
            r.stop()


# ---------------------------------------------------------------------------
# 7. get_paging_credentials() module function
# ---------------------------------------------------------------------------


class TestGetPagingCredentials:
    def test_returns_dict_with_all_three_keys(self) -> None:
        creds = get_paging_credentials()
        assert set(creds.keys()) == {"bot_token", "chat_id", "chat_id_ops"}

    def test_values_are_strings(self) -> None:
        creds = get_paging_credentials()
        for v in creds.values():
            assert isinstance(v, str)


# ---------------------------------------------------------------------------
# 8. Credential refresh on SM change
# ---------------------------------------------------------------------------


class TestCredentialRefresh:
    def test_refresh_updates_bot_token(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        sm_mock = _make_sm_mock(bot="tok-v1")
        with patch("alerting_service.config_reloaders.SecretManagerClient", return_value=sm_mock):
            r.start(project_id="test-project")
            assert r.get_bot_token() == "tok-v1"
            sm_mock.get_secrets.return_value = {
                _SM_BOT_TOKEN: "tok-v2",
                _SM_CHAT_ID: "chat-123",
                _SM_CHAT_ID_OPS: "ops-456",
            }
            with patch("alerting_service.config_reloaders.log_event"):
                r._refresh()
            assert r.get_bot_token() == "tok-v2"
        r.stop()

    def test_refresh_emits_config_changed_when_credentials_change(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        sm_mock = _make_sm_mock(bot="tok-v1")
        with patch("alerting_service.config_reloaders.SecretManagerClient", return_value=sm_mock):
            r.start(project_id="test-project")
            sm_mock.get_secrets.return_value = {_SM_BOT_TOKEN: "tok-v2", _SM_CHAT_ID: "chat-123"}
            with patch("alerting_service.config_reloaders.log_event") as mock_log:
                r._refresh()
            mock_log.assert_called_once()
            call_kwargs = mock_log.call_args
            event_name = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("event")
            assert event_name == "CONFIG_CHANGED"
        r.stop()

    def test_refresh_no_log_event_when_nothing_changes(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        sm_mock = _make_sm_mock(bot="tok-stable")
        with patch("alerting_service.config_reloaders.SecretManagerClient", return_value=sm_mock):
            r.start(project_id="test-project")
            with patch("alerting_service.config_reloaders.log_event") as mock_log:
                r._refresh()
            mock_log.assert_not_called()
        r.stop()


# ---------------------------------------------------------------------------
# 9. Thread safety — concurrent reads during refresh
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_reads_do_not_raise(self) -> None:
        r = _PagingCredentialsReloader(refresh_interval=9999)
        sm_mock = _make_sm_mock()
        with patch("alerting_service.config_reloaders.SecretManagerClient", return_value=sm_mock):
            r.start(project_id="test-project")
        errors: list[Exception] = []

        def reader() -> None:
            for _ in range(200):
                try:
                    _ = r.get_bot_token()
                    _ = r.get_chat_id()
                    _ = r.get_chat_id_ops()
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)
        finally:
            r.stop()

        assert not errors, f"Concurrent reads raised errors: {errors}"
