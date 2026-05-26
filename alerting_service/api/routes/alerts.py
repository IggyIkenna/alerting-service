import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from alerting_service.api.routes.mock_data import MOCK_SSE_EVENTS
from alerting_service.api.routes.mock_state import get_store as get_mock_store
from alerting_service.config import AlertingSystemConfig
from alerting_service.core.alert_store import AlertStore

router = APIRouter()
_store: AlertStore | None = None
_cfg = AlertingSystemConfig()


def set_alert_store(store: AlertStore) -> None:
    global _store
    _store = store


def get_store() -> AlertStore:
    if _store is None:
        raise RuntimeError("AlertStore not initialized")
    return _store


@router.get("/stream/alerts")
async def stream_alerts(store: Annotated[AlertStore, Depends(get_store)]) -> EventSourceResponse:
    if _cfg.is_mock_mode():
        return EventSourceResponse(_mock_sse_generator())
    return EventSourceResponse(_live_sse_generator(store))


async def _mock_sse_generator() -> AsyncIterator[dict[str, str]]:
    for evt in MOCK_SSE_EVENTS:
        yield {"data": json.dumps(evt)}
        await asyncio.sleep(1)
    while True:
        await asyncio.sleep(30)
        yield {"data": json.dumps({"heartbeat": True})}


async def _live_sse_generator(store: AlertStore) -> AsyncIterator[dict[str, str]]:
    q = store.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30)
                yield {"data": event.model_dump_json()}
            except TimeoutError:
                yield {"data": json.dumps({"heartbeat": True})}
    finally:
        store.unsubscribe(q)


@router.get("/rules/recent")
async def get_recent_alerts(store: Annotated[AlertStore, Depends(get_store)]) -> object:
    if _cfg.is_mock_mode():
        return get_mock_store().list("alerts")
    return store.get_recent_events(limit=100)


@router.post("/rules/recent")
async def create_alert(alert: dict[str, object]) -> dict[str, object]:
    """Record a new alert. Persists in mock state when CLOUD_MOCK_MODE=true."""
    if _cfg.is_mock_mode():
        if "alert_id" not in alert:
            alert["alert_id"] = f"alert-{uuid.uuid4().hex[:6]}"
        alert["id"] = alert["alert_id"]
        return get_mock_store().create("alerts", alert)
    return alert
