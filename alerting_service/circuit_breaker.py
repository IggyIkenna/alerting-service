"""Per-service, per-venue circuit breaker based on error event rate.

Tracks SERVICE_ERROR events in a sliding time window. When the error count
exceeds the threshold, the circuit transitions from CLOSED -> OPEN and
emits a CIRCUIT_OPEN lifecycle event for downstream consumers (e.g.
execution-service reads this to halt order submission to a failing venue).

State machine:
    CLOSED  -- errors > threshold --> OPEN
    OPEN    -- cooldown elapsed   --> HALF_OPEN
    HALF_OPEN -- success          --> CLOSED
    HALF_OPEN -- error            --> OPEN

Thread safety: single-threaded async context; no locks needed.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Circuit states — match LifecycleEventType values
STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Per-service, per-venue circuit breaker based on error event rate.

    Args:
        window_seconds: Sliding window for counting errors.
        threshold: Number of errors in the window that triggers OPEN.
        cooldown_seconds: Time after OPEN before transitioning to HALF_OPEN.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        threshold: int = 5,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self._errors: dict[str, list[float]] = {}  # key -> list of timestamps
        self._states: dict[str, str] = {}  # key -> CLOSED|OPEN|HALF_OPEN
        self._open_since: dict[str, float] = {}  # key -> timestamp when OPEN started
        self._window = window_seconds
        self._threshold = threshold
        self._cooldown = cooldown_seconds

    @staticmethod
    def _make_key(service: str, venue: str | None) -> str:
        """Build a composite key for the circuit state map."""
        return f"{service}:{venue or 'global'}"

    def _prune_old_errors(self, key: str, now: float) -> None:
        """Remove error timestamps older than the sliding window."""
        if key not in self._errors:
            return
        cutoff = now - self._window
        self._errors[key] = [ts for ts in self._errors[key] if ts > cutoff]

    def get_state(self, service: str, venue: str | None = None) -> str:
        """Get current circuit state for service+venue."""
        key = self._make_key(service, venue)
        return self._states.get(key, STATE_CLOSED)

    def record_error(self, service: str, venue: str | None = None) -> str:
        """Record an error and evaluate whether to transition state.

        Returns:
            The new state string if a transition occurred (e.g. "OPEN"),
            or empty string if no transition happened.
        """
        key = self._make_key(service, venue)
        now = time.monotonic()

        # Initialize if first time
        if key not in self._errors:
            self._errors[key] = []
        if key not in self._states:
            self._states[key] = STATE_CLOSED

        current_state = self._states[key]

        # If HALF_OPEN and we get another error -> back to OPEN
        if current_state == STATE_HALF_OPEN:
            self._states[key] = STATE_OPEN
            self._open_since[key] = now
            logger.warning(
                "Circuit %s: HALF_OPEN -> OPEN (error during probe)",
                key,
            )
            return STATE_OPEN

        # Record the error timestamp
        self._errors[key].append(now)
        self._prune_old_errors(key, now)

        # Check threshold for CLOSED -> OPEN
        if current_state == STATE_CLOSED and len(self._errors[key]) >= self._threshold:
            self._states[key] = STATE_OPEN
            self._open_since[key] = now
            logger.warning(
                "Circuit %s: CLOSED -> OPEN (%d errors in %.0fs window)",
                key,
                len(self._errors[key]),
                self._window,
            )
            return STATE_OPEN

        return ""

    def attempt_half_open(self, service: str, venue: str | None = None) -> bool:
        """Transition OPEN -> HALF_OPEN if cooldown has elapsed.

        Returns True if the transition happened, False otherwise.
        """
        key = self._make_key(service, venue)
        if self._states.get(key) != STATE_OPEN:
            return False

        now = time.monotonic()
        opened_at = self._open_since.get(key, now)
        if (now - opened_at) < self._cooldown:
            return False

        self._states[key] = STATE_HALF_OPEN
        logger.info("Circuit %s: OPEN -> HALF_OPEN (cooldown elapsed)", key)
        return True

    def record_success(self, service: str, venue: str | None = None) -> str:
        """Record a success. If HALF_OPEN, transition to CLOSED.

        Returns:
            The new state string if a transition occurred (e.g. "CLOSED"),
            or empty string if no transition happened.
        """
        key = self._make_key(service, venue)
        current_state = self._states.get(key, STATE_CLOSED)

        if current_state == STATE_HALF_OPEN:
            self._states[key] = STATE_CLOSED
            self._errors.pop(key, None)
            self._open_since.pop(key, None)
            logger.info("Circuit %s: HALF_OPEN -> CLOSED (success)", key)
            return STATE_CLOSED

        return ""

    def get_all_states(self) -> dict[str, str]:
        """Return a snapshot of all tracked circuit states."""
        return dict(self._states)

    def get_error_count(self, service: str, venue: str | None = None) -> int:
        """Return current error count in the sliding window for a key."""
        key = self._make_key(service, venue)
        now = time.monotonic()
        self._prune_old_errors(key, now)
        return len(self._errors.get(key, []))
