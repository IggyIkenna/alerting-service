"""
Alert deduplication with TTL-based expiry.

Prevents the same alert from being routed multiple times within a
configurable time window.  The dedup key is derived from the event
name and a hash of its details dictionary.
"""

import hashlib
import json
import time


class AlertDeduplicator:
    """TTL-based alert deduplicator.

    Each unique ``(event_name, hash_of_details)`` pair is remembered for
    ``ttl_seconds``.  Calling ``is_duplicate`` within that window returns
    ``True`` and suppresses the alert; after the TTL expires the same
    alert is treated as new.

    Args:
        ttl_seconds: How long (in seconds) a seen alert is considered a
            duplicate.  Defaults to 60.
    """

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        # Maps dedup key -> timestamp of first occurrence
        self._seen: dict[str, float] = {}

    @staticmethod
    def _make_key(event_name: str, details: dict[str, object]) -> str:
        """Build a dedup key from event name and a stable hash of details."""
        details_json = json.dumps(details, sort_keys=True, default=str)
        details_hash = hashlib.sha256(details_json.encode("utf-8")).hexdigest()[:16]
        return f"{event_name}:{details_hash}"

    def _evict_expired(self, now: float) -> None:
        """Remove entries older than TTL."""
        expired = [key for key, ts in self._seen.items() if (now - ts) > self._ttl]
        for key in expired:
            del self._seen[key]

    def is_duplicate(self, event_name: str, details: dict[str, object]) -> bool:
        """Check whether an alert is a duplicate within the TTL window.

        If the alert has not been seen (or its TTL expired), records it
        and returns ``False``.  If it was already seen within the TTL,
        returns ``True``.

        Args:
            event_name: Canonical event name (e.g. ``"KILL_SWITCH_ACTIVATED"``).
            details: Event details dictionary.

        Returns:
            ``True`` if the alert is a duplicate, ``False`` otherwise.
        """
        now = time.monotonic()
        self._evict_expired(now)

        key = self._make_key(event_name, details)
        if key in self._seen:
            return True

        self._seen[key] = now
        return False
