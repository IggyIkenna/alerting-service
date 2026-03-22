import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from unified_internal_contracts import AlertEvent
from unified_trading_library import classify_and_emit_error

from ..persistence.storage_store import AlertStorageStore

logger = logging.getLogger(__name__)


class AlertStore:
    def __init__(self, storage_store: AlertStorageStore | None = None) -> None:
        self._recent_events: list[AlertEvent] = []
        self._last_fired: dict[str, datetime] = {}
        self._subscribers: list[asyncio.Queue[AlertEvent]] = []
        self.storage_store = storage_store

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

        # Dual-write: persist to GCS if configured
        if self.storage_store is not None:
            try:
                event_dict: dict[str, object] = {
                    "alert_id": event.alert_id,
                    "rule_id": event.rule_id,
                    "triggered_at": event.triggered_at.isoformat(),
                    "severity": event.severity,
                    "message": event.message,
                    "metric_value": event.metric_value,
                    "threshold": event.threshold,
                }
                self.storage_store.write_alert_history(event_dict)
            except Exception as exc:
                classify_and_emit_error(
                    exc,
                    service_name="alerting-service",
                    operation="gcs_dual_write",
                )

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
