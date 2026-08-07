"""Unit tests for manifest-consolidator liveness + consolidation-failure rules."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from alerting_service.circuit_breaker import CircuitBreaker
from alerting_service.rules import consolidator_rules
from alerting_service.rules.consolidator_rules import (
    handle_consolidator_down_payload,
    handle_consolidator_recovered_payload,
    handle_manifest_consolidation_failed_payload,
    route_consolidator_event,
)
from alerting_service.subscribers.alert_subscriber import AlertSubscriber

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_route(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the explicit-channel router sink."""
    mock = MagicMock()
    monkeypatch.setattr(consolidator_rules, "route_event_with_explicit_channels", mock)
    return mock


@pytest.fixture
def mock_log_event() -> Iterator[MagicMock]:
    """Suppress log_event calls."""
    with patch("alerting_service.rules.consolidator_rules.log_event") as mock:
        yield mock


@pytest.fixture(autouse=True)
def _fresh_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a fresh repeat-failure breaker (no cross-test leakage)."""
    monkeypatch.setattr(
        consolidator_rules,
        "_consolidation_failure_breaker",
        CircuitBreaker(window_seconds=1800.0, threshold=3, cooldown_seconds=1800.0),
    )


def _down_payload(bucket: str = "market-data-tick-defi-prd-x") -> dict[str, object]:
    return {
        "bucket": bucket,
        "heartbeat_age_sec": 660.0,
        "max_age_sec": 300,
        "detail": (
            f"manifest consolidator heartbeat for {bucket} is 660s old (> 300s) "
            "while per-VM shards exist — consolidator is behind or DOWN"
        ),
    }


def _failed_payload(bucket: str = "market-data-tick-defi-prd-x") -> dict[str, object]:
    return {"bucket": bucket, "error": "boom: shard merge raised"}


# ---------------------------------------------------------------------------
# CONSOLIDATOR_DOWN → CRITICAL page
# ---------------------------------------------------------------------------


class TestConsolidatorDown:
    def test_routes_critical_to_paging_channels(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        handle_consolidator_down_payload(_down_payload())

        mock_route.assert_called_once()
        event_name, details = mock_route.call_args.args
        assert event_name == "CONSOLIDATOR_DOWN"
        assert mock_route.call_args.kwargs["channels"] == {"pagerduty", "telegram"}
        assert mock_route.call_args.kwargs["pd_severity"] == "critical"
        assert details["severity"] == "CRITICAL"

    def test_payload_carries_bucket_and_detail_message(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        handle_consolidator_down_payload(_down_payload(bucket="bkt-1"))

        _, details = mock_route.call_args.args
        assert details["bucket"] == "bkt-1"
        assert "consolidator is behind or DOWN" in str(details["message"])
        assert details["source"] == "alerting-service/consolidator-rules"

    def test_default_message_when_detail_absent(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        handle_consolidator_down_payload({"bucket": "bkt-2"})

        _, details = mock_route.call_args.args
        assert details["message"] == "manifest consolidator DOWN for bucket=bkt-2"


# ---------------------------------------------------------------------------
# MANIFEST_CONSOLIDATION_FAILED → WARN, escalating to CRITICAL on repeat
# ---------------------------------------------------------------------------


class TestManifestConsolidationFailed:
    def test_first_failure_is_warn_telegram_only(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        handle_manifest_consolidation_failed_payload(_failed_payload())

        mock_route.assert_called_once()
        event_name, details = mock_route.call_args.args
        assert event_name == "MANIFEST_CONSOLIDATION_FAILED"
        assert mock_route.call_args.kwargs["channels"] == {"telegram"}
        assert mock_route.call_args.kwargs["pd_severity"] is None
        assert details["severity"] == "WARN"
        assert details["failures_in_window"] == 1
        assert "escalation_reason" not in details

    def test_repeat_failures_escalate_to_critical_page(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        for _ in range(3):
            handle_manifest_consolidation_failed_payload(_failed_payload())

        # 1st + 2nd fire WARN; 3rd crosses the breaker threshold → CRITICAL page.
        severities = [call.args[1]["severity"] for call in mock_route.call_args_list]
        assert severities == ["WARN", "WARN", "CRITICAL"]
        last = mock_route.call_args
        assert last.kwargs["channels"] == {"pagerduty", "telegram"}
        assert last.kwargs["pd_severity"] == "critical"
        assert "crash-looping" in str(last.args[1]["escalation_reason"])

    def test_failures_keep_paging_critical_while_circuit_open(
        self, mock_route: MagicMock, mock_log_event: MagicMock
    ) -> None:
        for _ in range(4):
            handle_manifest_consolidation_failed_payload(_failed_payload())

        # 4th failure (circuit already OPEN) stays CRITICAL.
        assert mock_route.call_args.args[1]["severity"] == "CRITICAL"
        assert mock_route.call_args.kwargs["pd_severity"] == "critical"

    def test_repeat_window_is_keyed_per_bucket(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        # 2 failures each on two distinct buckets — neither crosses threshold=3.
        for bucket in ("bkt-a", "bkt-b", "bkt-a", "bkt-b"):
            handle_manifest_consolidation_failed_payload(_failed_payload(bucket=bucket))

        severities = [call.args[1]["severity"] for call in mock_route.call_args_list]
        assert severities == ["WARN", "WARN", "WARN", "WARN"]


# ---------------------------------------------------------------------------
# CONSOLIDATOR_RECOVERED → INFO, notify-only (the RESOLVED half, 2026-08-06)
# ---------------------------------------------------------------------------


class TestConsolidatorRecovered:
    def test_routes_info_to_telegram_only_no_page(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        handle_consolidator_recovered_payload({"bucket": "bkt-1"})

        mock_route.assert_called_once()
        event_name, details = mock_route.call_args.args
        assert event_name == "CONSOLIDATOR_RECOVERED"
        assert mock_route.call_args.kwargs["channels"] == {"telegram"}
        assert mock_route.call_args.kwargs["pd_severity"] is None
        assert details["severity"] == "INFO"

    def test_payload_carries_bucket_and_default_message(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        handle_consolidator_recovered_payload({"bucket": "bkt-2"})

        _, details = mock_route.call_args.args
        assert details["bucket"] == "bkt-2"
        assert details["message"] == "manifest consolidator RECOVERED for bucket=bkt-2"

    def test_never_dispatches_to_pagerduty_channel(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        handle_consolidator_recovered_payload({"bucket": "bkt-3"})

        assert "pagerduty" not in mock_route.call_args.kwargs["channels"]


# ---------------------------------------------------------------------------
# Dispatch: route_consolidator_event + subscriber typed-handler wiring
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_route_consolidator_event_dispatches_down(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        route_consolidator_event("CONSOLIDATOR_DOWN", _down_payload())

        assert mock_route.call_args.args[0] == "CONSOLIDATOR_DOWN"

    def test_route_consolidator_event_dispatches_failed(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        route_consolidator_event("MANIFEST_CONSOLIDATION_FAILED", _failed_payload())

        assert mock_route.call_args.args[0] == "MANIFEST_CONSOLIDATION_FAILED"

    def test_unknown_event_produces_no_alert(self, mock_route: MagicMock, mock_log_event: MagicMock) -> None:
        route_consolidator_event("SOME_OTHER_EVENT", {"bucket": "bkt-x"})

        mock_route.assert_not_called()
        mock_log_event.assert_not_called()

    def test_route_consolidator_event_dispatches_recovered(
        self, mock_route: MagicMock, mock_log_event: MagicMock
    ) -> None:
        route_consolidator_event("CONSOLIDATOR_RECOVERED", {"bucket": "bkt-x"})

        assert mock_route.call_args.args[0] == "CONSOLIDATOR_RECOVERED"

    def test_subscriber_dispatches_consolidator_down_to_rule(
        self, mock_route: MagicMock, mock_log_event: MagicMock
    ) -> None:
        AlertSubscriber.dispatch_event("CONSOLIDATOR_DOWN", _down_payload())

        mock_route.assert_called_once()
        assert mock_route.call_args.args[0] == "CONSOLIDATOR_DOWN"
        assert mock_route.call_args.kwargs["pd_severity"] == "critical"

    def test_subscriber_dispatches_consolidation_failed_to_rule(
        self, mock_route: MagicMock, mock_log_event: MagicMock
    ) -> None:
        AlertSubscriber.dispatch_event("MANIFEST_CONSOLIDATION_FAILED", _failed_payload())

        mock_route.assert_called_once()
        assert mock_route.call_args.args[0] == "MANIFEST_CONSOLIDATION_FAILED"
        assert mock_route.call_args.args[1]["severity"] == "WARN"

    def test_subscriber_dispatches_consolidator_recovered_to_rule(
        self, mock_route: MagicMock, mock_log_event: MagicMock
    ) -> None:
        AlertSubscriber.dispatch_event("CONSOLIDATOR_RECOVERED", {"bucket": "bkt-x"})

        mock_route.assert_called_once()
        assert mock_route.call_args.args[0] == "CONSOLIDATOR_RECOVERED"
        assert mock_route.call_args.kwargs["pd_severity"] is None
