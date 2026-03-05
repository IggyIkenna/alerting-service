import asyncio
import contextlib
from datetime import UTC, datetime

from unified_internal_contracts import AlertEvent


class AlertStore:
    def __init__(self) -> None:
        self._recent_events: list[AlertEvent] = []
        self._last_fired: dict[str, datetime] = {}
        self._subscribers: list[asyncio.Queue[AlertEvent]] = []

    def is_cooled_down(self, rule_id: str, cooldown_seconds: int) -> bool:
        last = self._last_fired.get(rule_id)
        if last is None:
            return True
        return (datetime.now(UTC) - last).total_seconds() > cooldown_seconds

    def record_fired(self, event: AlertEvent) -> None:
        self._last_fired[event.rule_id] = event.triggered_at
        self._recent_events.append(event)
        if len(self._recent_events) > 1000:
            self._recent_events = self._recent_events[-500:]

    def get_recent_events(self, limit: int = 100) -> list[AlertEvent]:
        return self._recent_events[-limit:]

    def subscribe(self) -> asyncio.Queue[AlertEvent]:
        q: asyncio.Queue[AlertEvent] = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[AlertEvent]) -> None:
        self._subscribers.remove(q)

    async def publish(self, event: AlertEvent) -> None:
        for q in self._subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)
