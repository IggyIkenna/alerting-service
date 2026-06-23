"""Unit tests for the tick-staleness / connectivity-gap coalesce window.

Per alerting_service_live_rules_2026_05_07 § "Tick-staleness + connectivity-gap
event taxonomy": when both TICK_STALENESS (MDPS) and CONNECTIVITY_GAP_DETECTED
(MTDS) fire on the same (venue, instrument) within a 30-second window,
operators see ONE merged alert rather than two separate fires.

Coalesce key: ``(venue, instrument)`` extracted from the alert payload
``details`` dict via the ``_coalesce_key`` helper. Closed-set of coalesced
event names: ``{TICK_STALENESS, CONNECTIVITY_GAP_DETECTED}``. Other alerts
pass through unchanged.

Coalesce window state lives in module-level ``_coalesce_window`` dict; tests
use ``_reset_coalesce_window_for_tests()`` between cases to ensure isolation.
"""

from unittest.mock import MagicMock, patch

import pytest

from alerting_service.notifiers.router import (
    _COALESCE_WINDOW_SECONDS,
    _COALESCED_EVENT_NAMES,
    _check_coalesce_window,
    _coalesce_key,
    _reset_coalesce_window_for_tests,
    route_event,
)


@pytest.fixture(autouse=True)
def _reset_coalesce_window() -> None:
    """Reset coalesce window state before every test."""
    _reset_coalesce_window_for_tests()


@pytest.fixture
def mock_route_dependencies():
    """Patch all heavy router dependencies so route_event() is unit-testable."""
    mock_cfg = MagicMock()
    mock_cfg.telegram_bot_token = "bot-token-123"
    mock_cfg.telegram_chat_id = "chat-456"
    mock_cfg.gcp_project_id = "test-project"
    mock_cfg.pagerduty_disabled = False
    mock_cfg.quietness_baseline_mode = False
    mock_cfg.routing_rules = [
        {
            "event_pattern": "TICK_STALENESS",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "warning",
        },
        {
            "event_pattern": "CONNECTIVITY_GAP_DETECTED",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "warning",
        },
        {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
    ]
    with (
        patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=mock_cfg),
        patch("alerting_service.notifiers.router.pd_send_event", return_value=True) as mock_pd,
        patch("alerting_service.notifiers.router.send_uts_live_alert") as mock_tg,
        patch(
            "alerting_service.notifiers.router.get_paging_credentials",
            return_value={"uts_live_alerts_slack_webhook": "https://hooks.slack.com/services/T/B/test"},
        ),
        patch("alerting_service.notifiers.router.log_event"),
        patch("alerting_service.notifiers.router._persist_delivery_record"),
        patch("alerting_service.notifiers.router._persist_config_snapshot"),
        patch("alerting_service.notifiers.router._publish_kill_switch_event"),
    ):
        yield {"pd": mock_pd, "telegram": mock_tg, "config": mock_cfg}


class TestCoalesceKey:
    """Tests for the _coalesce_key extractor."""

    def test_extracts_venue_and_instrument(self) -> None:
        key = _coalesce_key(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT", "extra": "noise"},
        )
        assert key == ("binance", "BTC-USDT")

    def test_returns_none_when_venue_missing(self) -> None:
        key = _coalesce_key("TICK_STALENESS", {"instrument": "BTC-USDT"})
        assert key is None

    def test_returns_none_when_instrument_missing(self) -> None:
        key = _coalesce_key("TICK_STALENESS", {"venue": "binance"})
        assert key is None

    def test_returns_none_when_both_missing(self) -> None:
        key = _coalesce_key("TICK_STALENESS", {"message": "stale"})
        assert key is None

    def test_coerces_non_string_values_to_str(self) -> None:
        # Defensive: even if emitter sends int / Enum, we coerce + carry on
        key = _coalesce_key("TICK_STALENESS", {"venue": 123, "instrument": 456})
        assert key == ("123", "456")


class TestCoalescedEventNames:
    """The closed set of event names that participate in coalescing."""

    def test_tick_staleness_is_coalesced(self) -> None:
        assert "TICK_STALENESS" in _COALESCED_EVENT_NAMES

    def test_connectivity_gap_detected_is_coalesced(self) -> None:
        assert "CONNECTIVITY_GAP_DETECTED" in _COALESCED_EVENT_NAMES

    def test_recovery_events_not_coalesced(self) -> None:
        """Recovery events (CONNECTIVITY_RECOVERED, CONNECTIVITY_GAP_BACKFILLED)
        are INFO-severity Telegram-only fires; coalescing them would silently
        drop the recovery signal that closes the loop on the gap alert."""
        assert "CONNECTIVITY_RECOVERED" not in _COALESCED_EVENT_NAMES
        assert "CONNECTIVITY_GAP_BACKFILLED" not in _COALESCED_EVENT_NAMES

    def test_unrelated_events_not_coalesced(self) -> None:
        assert "CIRCUIT_BREAKER_OPEN" not in _COALESCED_EVENT_NAMES
        assert "KILL_SWITCH_VENUE_DISCONNECT" not in _COALESCED_EVENT_NAMES
        assert "DEFI_HEALTH_FACTOR_CRITICAL" not in _COALESCED_EVENT_NAMES


class TestCheckCoalesceWindow:
    """Tests for the _check_coalesce_window state-machine helper."""

    def test_first_fire_returns_false_does_not_suppress(self) -> None:
        suppressed = _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=100.0,
        )
        assert suppressed is False

    def test_second_fire_same_key_within_window_returns_true_suppresses(self) -> None:
        _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=100.0,
        )
        suppressed = _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=110.0,
        )
        assert suppressed is True

    def test_second_fire_different_key_does_not_suppress(self) -> None:
        _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=100.0,
        )
        # Different (venue, instrument) — should fire normally.
        suppressed = _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "ETH-USDT"},
            now_monotonic=110.0,
        )
        assert suppressed is False

    def test_second_fire_different_venue_does_not_suppress(self) -> None:
        _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=100.0,
        )
        suppressed = _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "kraken", "instrument": "BTC-USDT"},
            now_monotonic=110.0,
        )
        assert suppressed is False

    def test_fire_after_window_expires_does_not_suppress(self) -> None:
        """At t=0 fire, then at t=31s fire again — window is 30s so the second
        fire is a fresh entry, not a coalesce."""
        _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=0.0,
        )
        suppressed = _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=_COALESCE_WINDOW_SECONDS + 1.0,
        )
        assert suppressed is False

    def test_fire_exactly_at_window_boundary_does_not_suppress(self) -> None:
        """Boundary semantics: >= window means new entry; < window means
        coalesce. 30.0s exactly is NEW, 29.99s is coalesced."""
        _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=0.0,
        )
        suppressed = _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=_COALESCE_WINDOW_SECONDS,
        )
        assert suppressed is False

    def test_cross_event_type_coalesces_within_window(self) -> None:
        """The whole point: TICK_STALENESS + CONNECTIVITY_GAP_DETECTED for
        the same (venue, instrument) within 30s become ONE alert."""
        _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT", "actual_seconds": 600},
            now_monotonic=100.0,
        )
        suppressed = _check_coalesce_window(
            "CONNECTIVITY_GAP_DETECTED",
            {
                "venue": "binance",
                "instrument": "BTC-USDT",
                "gap_window_start": "2026-05-11T12:00:00Z",
            },
            now_monotonic=115.0,
        )
        assert suppressed is True

    def test_non_coalesced_event_passes_through(self) -> None:
        """CIRCUIT_BREAKER_OPEN is not in the coalesced set; never suppress."""
        suppressed = _check_coalesce_window(
            "CIRCUIT_BREAKER_OPEN",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=100.0,
        )
        assert suppressed is False

    def test_missing_venue_passes_through(self) -> None:
        """Coalesce key can't be built — preferred over silent merge."""
        suppressed = _check_coalesce_window(
            "TICK_STALENESS",
            {"instrument": "BTC-USDT"},  # No venue
            now_monotonic=100.0,
        )
        assert suppressed is False

    def test_recovery_event_passes_through(self) -> None:
        """CONNECTIVITY_RECOVERED is INFO-level recovery, not a gap — never coalesced."""
        _check_coalesce_window(
            "TICK_STALENESS",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=100.0,
        )
        suppressed = _check_coalesce_window(
            "CONNECTIVITY_RECOVERED",
            {"venue": "binance", "instrument": "BTC-USDT"},
            now_monotonic=110.0,
        )
        assert suppressed is False


class TestRouteEventCoalesce:
    """End-to-end test that route_event() respects the coalesce window."""

    def test_two_tick_staleness_same_key_within_30s_only_first_fires(self, mock_route_dependencies) -> None:
        # Two TICK_STALENESS alerts for same (venue, instrument) within 30s —
        # only first fires (PagerDuty + Telegram); second merges silently.
        details_1: dict[str, object] = {
            "venue": "binance",
            "instrument": "BTC-USDT",
            "message": "first stale",
            "_test_seq": 1,
        }
        details_2: dict[str, object] = {
            "venue": "binance",
            "instrument": "BTC-USDT",
            "message": "second stale",
            "_test_seq": 2,
        }
        route_event("TICK_STALENESS", details_1)
        route_event("TICK_STALENESS", details_2)
        # PagerDuty was called exactly once (first fire only).
        assert mock_route_dependencies["pd"].call_count == 1
        # Telegram was called exactly once.
        assert mock_route_dependencies["telegram"].call_count == 1

    def test_two_tick_staleness_different_keys_both_fire(self, mock_route_dependencies) -> None:
        # Two TICK_STALENESS alerts for DIFFERENT (venue, instrument) → both fire.
        details_1: dict[str, object] = {
            "venue": "binance",
            "instrument": "BTC-USDT",
            "message": "binance stale",
        }
        details_2: dict[str, object] = {
            "venue": "kraken",
            "instrument": "BTC-USDT",
            "message": "kraken stale",
        }
        route_event("TICK_STALENESS", details_1)
        route_event("TICK_STALENESS", details_2)
        assert mock_route_dependencies["pd"].call_count == 2
        assert mock_route_dependencies["telegram"].call_count == 2

    def test_non_staleness_alerts_pass_through_without_coalescing(self, mock_route_dependencies) -> None:
        # CIRCUIT_BREAKER_OPEN is not coalesced — each fire dispatches.
        # (Two distinct details to bypass the AlertDeduplicator hash check.)
        route_event(
            "CIRCUIT_BREAKER_OPEN",
            {"venue": "binance", "instrument": "BTC-USDT", "seq": 1},
        )
        route_event(
            "CIRCUIT_BREAKER_OPEN",
            {"venue": "binance", "instrument": "BTC-USDT", "seq": 2},
        )
        # Catch-all rule routes to telegram only (no PD).
        assert mock_route_dependencies["telegram"].call_count == 2
