"""Unit tests for the active feed self-healing recovery decision tree.

Phase 2 of
``plans/active/data_feed_sla_registry_and_active_self_healing_2026_06_19.md``.
Mirrors test_consolidator_rules: fresh breaker per test + mocked router sink.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from alerting_service.circuit_breaker import CircuitBreaker
from alerting_service.rules import feed_refetch_rules
from alerting_service.rules.feed_refetch_rules import (
    evaluate_stale_critical_feed,
    handle_refetch_failed,
)


@pytest.fixture
def mock_route(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(feed_refetch_rules, "route_event_with_explicit_channels", mock)
    return mock


@pytest.fixture
def mock_log_event() -> Iterator[MagicMock]:
    with patch("alerting_service.rules.feed_refetch_rules.log_event") as mock:
        yield mock


@pytest.fixture(autouse=True)
def _fresh_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        feed_refetch_rules,
        "_refetch_failure_breaker",
        CircuitBreaker(window_seconds=1800.0, threshold=3, cooldown_seconds=1800.0),
    )


# ---------------------------------------------------------------------------
# (a)+(b) stale feed → SILENT_RETRY, orders already blocked
# ---------------------------------------------------------------------------


def test_stale_critical_feed_blocks_orders_and_fires_refetch(mock_log_event: MagicMock) -> None:
    decision = evaluate_stale_critical_feed("binance", "critical")
    assert decision["orders_blocked_by_freshness_gate"] is True  # (a) gate stays closed
    assert decision["refetch_action"] == "refetch-feed:binance"  # (b) bound SILENT_RETRY
    assert decision["recovery_step"] == "SILENT_RETRY"


def test_stale_important_feed_does_not_block_orders(mock_log_event: MagicMock) -> None:
    decision = evaluate_stale_critical_feed("pinnacle", "important")
    assert decision["orders_blocked_by_freshness_gate"] is False
    assert decision["refetch_action"] == "refetch-feed:pinnacle"


# ---------------------------------------------------------------------------
# (c) repeated refetch failure escalates WARN → CRITICAL
# ---------------------------------------------------------------------------


def test_single_refetch_failure_is_warn_no_advisory(mock_route: MagicMock, mock_log_event: MagicMock) -> None:
    alert = handle_refetch_failed("binance", "critical")
    assert alert["severity"] == "WARN"
    assert alert["recovery_step"] == "ESCALATE"
    assert "advisory" not in alert
    # WARN → telegram only, no pagerduty
    _, kwargs = mock_route.call_args
    assert kwargs["pd_severity"] is None
    assert kwargs["channels"] == {"telegram"}


def test_repeated_failure_escalates_to_critical_with_advisory(
    mock_route: MagicMock, mock_log_event: MagicMock
) -> None:
    # threshold=3 → 3rd failure opens the breaker → CRITICAL + advisory.
    handle_refetch_failed("binance", "critical")
    handle_refetch_failed("binance", "critical")
    alert = handle_refetch_failed("binance", "critical")
    assert alert["severity"] == "CRITICAL"
    assert alert["recovery_step"] == "ADVISE_POSITION_REDUCTION"  # (d)
    assert alert["advisory"] == "reduce_position"
    assert alert["failures_in_window"] == 3
    assert "escalation_reason" in alert
    # CRITICAL → page (pagerduty + telegram)
    _, kwargs = mock_route.call_args
    assert kwargs["pd_severity"] == "critical"
    assert kwargs["channels"] == {"pagerduty", "telegram"}


def test_important_feed_escalates_to_high_not_critical(mock_route: MagicMock, mock_log_event: MagicMock) -> None:
    handle_refetch_failed("pinnacle", "important")
    handle_refetch_failed("pinnacle", "important")
    alert = handle_refetch_failed("pinnacle", "important")
    assert alert["severity"] == "HIGH"  # important never pages CRITICAL
    assert alert["recovery_step"] == "ADVISE_POSITION_REDUCTION"
    _, kwargs = mock_route.call_args
    assert kwargs["pd_severity"] == "warning"


# ---------------------------------------------------------------------------
# audit-ack SLA cadence mapped to criticality
# ---------------------------------------------------------------------------


def test_audit_ack_sla_tighter_for_critical(mock_route: MagicMock, mock_log_event: MagicMock) -> None:
    # critical (escalated CRITICAL) SLA must be tighter than important (HIGH).
    handle_refetch_failed("binance", "critical")
    handle_refetch_failed("binance", "critical")
    crit = handle_refetch_failed("binance", "critical")
    handle_refetch_failed("pinnacle", "important")
    handle_refetch_failed("pinnacle", "important")
    imp = handle_refetch_failed("pinnacle", "important")
    assert int(crit["audit_ack_due_seconds"]) < int(imp["audit_ack_due_seconds"])


# ---------------------------------------------------------------------------
# per-feed isolation — one feed's failures don't escalate another
# ---------------------------------------------------------------------------


def test_failures_are_isolated_per_feed(mock_route: MagicMock, mock_log_event: MagicMock) -> None:
    handle_refetch_failed("binance", "critical")
    handle_refetch_failed("binance", "critical")
    # A failure on a DIFFERENT feed is its own (1st) failure → WARN, not CRITICAL.
    other = handle_refetch_failed("okx", "critical")
    assert other["severity"] == "WARN"
    assert other["failures_in_window"] == 1
