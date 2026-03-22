"""Unit tests for the CircuitBreaker module."""

from __future__ import annotations

import time
from unittest.mock import patch

from alerting_service.circuit_breaker import (
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    CircuitBreaker,
)


class TestCircuitBreakerBasics:
    def test_initial_state_is_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.get_state("svc-a") == STATE_CLOSED

    def test_errors_below_threshold_stay_closed(self) -> None:
        cb = CircuitBreaker(threshold=5)
        for _ in range(4):
            result = cb.record_error("svc-a", "binance")
            assert result == ""
        assert cb.get_state("svc-a", "binance") == STATE_CLOSED

    def test_errors_at_threshold_open_circuit(self) -> None:
        cb = CircuitBreaker(threshold=3)
        for _ in range(2):
            cb.record_error("svc-a", "binance")
        result = cb.record_error("svc-a", "binance")
        assert result == STATE_OPEN
        assert cb.get_state("svc-a", "binance") == STATE_OPEN

    def test_different_keys_are_independent(self) -> None:
        cb = CircuitBreaker(threshold=2)
        cb.record_error("svc-a", "binance")
        cb.record_error("svc-a", "binance")
        assert cb.get_state("svc-a", "binance") == STATE_OPEN
        assert cb.get_state("svc-a", "coinbase") == STATE_CLOSED
        assert cb.get_state("svc-b", "binance") == STATE_CLOSED

    def test_global_venue_key(self) -> None:
        cb = CircuitBreaker(threshold=2)
        cb.record_error("svc-a", None)
        cb.record_error("svc-a", None)
        assert cb.get_state("svc-a", None) == STATE_OPEN
        assert cb.get_state("svc-a") == STATE_OPEN


class TestCircuitBreakerSlidingWindow:
    def test_old_errors_expire_from_window(self) -> None:
        cb = CircuitBreaker(window_seconds=1.0, threshold=3)
        # Record 2 errors
        cb.record_error("svc", "v")
        cb.record_error("svc", "v")

        # Simulate time passing beyond window by patching monotonic
        real_monotonic = time.monotonic
        with patch("alerting_service.circuit_breaker.time.monotonic") as mock_time:
            # First two errors are now expired; new error should not trigger
            mock_time.return_value = real_monotonic() + 2.0
            result = cb.record_error("svc", "v")
            assert result == ""
            assert cb.get_state("svc", "v") == STATE_CLOSED

    def test_error_count_reflects_window(self) -> None:
        cb = CircuitBreaker(window_seconds=60.0, threshold=10)
        for _ in range(5):
            cb.record_error("svc", "v")
        assert cb.get_error_count("svc", "v") == 5


class TestCircuitBreakerHalfOpen:
    def test_half_open_after_cooldown(self) -> None:
        cb = CircuitBreaker(threshold=2, cooldown_seconds=1.0)
        cb.record_error("svc", "v")
        cb.record_error("svc", "v")
        assert cb.get_state("svc", "v") == STATE_OPEN

        # Before cooldown
        assert cb.attempt_half_open("svc", "v") is False

        # After cooldown
        real_monotonic = time.monotonic
        with patch("alerting_service.circuit_breaker.time.monotonic") as mock_time:
            mock_time.return_value = real_monotonic() + 2.0
            assert cb.attempt_half_open("svc", "v") is True
            assert cb.get_state("svc", "v") == STATE_HALF_OPEN

    def test_half_open_not_applied_to_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.attempt_half_open("svc", "v") is False

    def test_success_in_half_open_closes_circuit(self) -> None:
        cb = CircuitBreaker(threshold=1, cooldown_seconds=0.0)
        cb.record_error("svc", "v")
        assert cb.get_state("svc", "v") == STATE_OPEN

        cb.attempt_half_open("svc", "v")
        assert cb.get_state("svc", "v") == STATE_HALF_OPEN

        result = cb.record_success("svc", "v")
        assert result == STATE_CLOSED
        assert cb.get_state("svc", "v") == STATE_CLOSED

    def test_error_in_half_open_reopens_circuit(self) -> None:
        cb = CircuitBreaker(threshold=1, cooldown_seconds=0.0)
        cb.record_error("svc", "v")
        cb.attempt_half_open("svc", "v")
        assert cb.get_state("svc", "v") == STATE_HALF_OPEN

        result = cb.record_error("svc", "v")
        assert result == STATE_OPEN
        assert cb.get_state("svc", "v") == STATE_OPEN

    def test_success_in_closed_state_is_noop(self) -> None:
        cb = CircuitBreaker()
        result = cb.record_success("svc", "v")
        assert result == ""
        assert cb.get_state("svc", "v") == STATE_CLOSED


class TestGetAllStates:
    def test_empty_initially(self) -> None:
        cb = CircuitBreaker()
        assert cb.get_all_states() == {}

    def test_reflects_recorded_states(self) -> None:
        cb = CircuitBreaker(threshold=1)
        cb.record_error("svc-a", "v1")
        cb.record_error("svc-b", None)
        states = cb.get_all_states()
        assert states["svc-a:v1"] == STATE_OPEN
        assert states["svc-b:global"] == STATE_OPEN
