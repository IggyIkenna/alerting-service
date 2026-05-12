# ruff: noqa: N802 - test method names use NOT in upper case for visual contrast
"""Tests for Phase 3.F synthetic-paging-suppression in alerting router.

When ``details["synthetic"] = True`` (or any of the truthy serialisations
in ``_SYNTHETIC_TRUTHY``), ``route_event`` and ``route_event_with_explicit_channels``
log + persist + return WITHOUT dispatching to PagerDuty / Telegram.

Real-fire alerts (``synthetic=False`` or absent) continue to page normally.
"""

import contextlib
from unittest.mock import patch

import pytest

from alerting_service.notifiers.router import (
    _is_synthetic,
    route_event,
    route_event_with_explicit_channels,
)


@pytest.fixture
def mock_pd_send_event():  # type: ignore[no-untyped-def]
    with patch("alerting_service.notifiers.router.pd_send_event", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_send_telegram():  # type: ignore[no-untyped-def]
    with patch("alerting_service.notifiers.router.send_telegram", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_log_event():  # type: ignore[no-untyped-def]
    with patch("alerting_service.notifiers.router.log_event") as mock:
        yield mock


@pytest.fixture
def mock_persist_delivery():  # type: ignore[no-untyped-def]
    with patch("alerting_service.notifiers.router._persist_delivery_record") as mock:
        yield mock


@pytest.fixture
def mock_persist_config():  # type: ignore[no-untyped-def]
    with patch("alerting_service.notifiers.router._persist_config_snapshot") as mock:
        yield mock


@pytest.fixture(autouse=True)
def _reset_deduplicator():  # type: ignore[no-untyped-def]
    """Ensure every test sees a fresh dedup cache (router uses TTL=60s)."""
    from alerting_service.notifiers.router import _deduplicator

    _deduplicator._seen.clear()  # type: ignore[reportPrivateUsage]
    yield


# ─── _is_synthetic helper ────────────────────────────────────────────────────


class TestIsSyntheticHelper:
    def test_bool_true(self) -> None:
        assert _is_synthetic({"synthetic": True}) is True

    def test_str_true_variants(self) -> None:
        for v in ("True", "true", "TRUE", "1"):
            assert _is_synthetic({"synthetic": v}) is True, f"failed for {v!r}"

    def test_bool_false(self) -> None:
        assert _is_synthetic({"synthetic": False}) is False

    def test_missing_key(self) -> None:
        assert _is_synthetic({}) is False

    def test_falsy_strings(self) -> None:
        for v in ("False", "false", "0", "", "no"):
            assert _is_synthetic({"synthetic": v}) is False, f"failed for {v!r}"


# ─── route_event synthetic suppression ───────────────────────────────────────


class TestRouteEventSyntheticSuppression:
    def test_synthetic_true_suppresses_pagerduty_and_telegram(
        self,
        mock_pd_send_event,  # type: ignore[no-untyped-def]
        mock_send_telegram,  # type: ignore[no-untyped-def]
        mock_log_event,  # type: ignore[no-untyped-def]
        mock_persist_delivery,  # type: ignore[no-untyped-def]
        mock_persist_config,  # type: ignore[no-untyped-def]
    ) -> None:
        del mock_persist_config  # unused — kept for fixture parity
        route_event(
            "KILL_SWITCH_ARMED",
            {"synthetic": True, "scenario_id": "cefi_funding_spike_10x"},
        )
        mock_pd_send_event.assert_not_called()
        mock_send_telegram.assert_not_called()
        # ALERT_SUPPRESSED_SYNTHETIC MUST appear in the log_event call set.
        suppressed_calls = [
            c for c in mock_log_event.call_args_list if c.args[0] == "ALERT_SUPPRESSED_SYNTHETIC"
        ]
        assert len(suppressed_calls) == 1
        # Record was persisted as log_only with synthetic-suppressed status.
        mock_persist_delivery.assert_called_once()
        record = mock_persist_delivery.call_args.args[0]
        assert record["channel"] == "log_only"
        assert record["status"] == "suppressed_synthetic"

    def test_synthetic_false_does_NOT_log_suppressed_event(
        self,
        mock_pd_send_event,  # type: ignore[no-untyped-def]
        mock_send_telegram,  # type: ignore[no-untyped-def]
        mock_log_event,  # type: ignore[no-untyped-def]
        mock_persist_delivery,  # type: ignore[no-untyped-def]
        mock_persist_config,  # type: ignore[no-untyped-def]
    ) -> None:
        """Real-fire alerts (synthetic=False) must NOT trigger the suppression
        short-circuit. We patch ``_get_cloud_config`` to a stub config so the
        non-synthetic path completes its config-bootstrap without GCP creds —
        the load-bearing assertion is that ALERT_SUPPRESSED_SYNTHETIC was
        NEVER logged.
        """
        del mock_pd_send_event, mock_send_telegram, mock_persist_delivery, mock_persist_config

        with patch("alerting_service.notifiers.router._get_cloud_config") as cfg:
            cfg.return_value.routing_rules = ()
            cfg.return_value.telegram_bot_token = ""
            cfg.return_value.telegram_chat_id = ""
            with contextlib.suppress(Exception):
                route_event(
                    "KILL_SWITCH_ARMED",
                    {"synthetic": False, "kill_switch_id": "KILL_ALL_LIVE"},
                )

        suppressed_calls = [
            c for c in mock_log_event.call_args_list if c.args[0] == "ALERT_SUPPRESSED_SYNTHETIC"
        ]
        assert suppressed_calls == [], (
            f"Real-fire path must NOT emit ALERT_SUPPRESSED_SYNTHETIC; got {len(suppressed_calls)}"
        )


# ─── route_event_with_explicit_channels synthetic suppression ────────────────


class TestRouteEventExplicitChannelsSyntheticSuppression:
    def test_synthetic_true_skips_explicit_channels(
        self,
        mock_pd_send_event,  # type: ignore[no-untyped-def]
        mock_send_telegram,  # type: ignore[no-untyped-def]
        mock_log_event,  # type: ignore[no-untyped-def]
        mock_persist_delivery,  # type: ignore[no-untyped-def]
        mock_persist_config,  # type: ignore[no-untyped-def]
    ) -> None:
        del mock_persist_config
        route_event_with_explicit_channels(
            "CIRCUIT_BREAKER_OPEN",
            {"synthetic": True, "breaker_id": "VENUE_OUTAGE_SECONDS"},
            channels={"pagerduty", "telegram"},
            pd_severity="warning",
        )
        mock_pd_send_event.assert_not_called()
        mock_send_telegram.assert_not_called()
        suppressed_calls = [
            c for c in mock_log_event.call_args_list if c.args[0] == "ALERT_SUPPRESSED_SYNTHETIC"
        ]
        assert len(suppressed_calls) == 1
        mock_persist_delivery.assert_called_once()
