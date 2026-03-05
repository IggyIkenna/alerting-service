import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from alerting_service.core.alert_store import AlertStore

router = APIRouter()
_store: AlertStore | None = None


def set_alert_store(store: AlertStore) -> None:
    global _store
    _store = store


def get_store() -> AlertStore:
    if _store is None:
        raise RuntimeError("AlertStore not initialized")
    return _store


@router.get("/stream/alerts")
async def stream_alerts(store: Annotated[AlertStore, Depends(get_store)]) -> EventSourceResponse:
    async def generator() -> AsyncIterator[dict[str, str]]:
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

    return EventSourceResponse(generator())


@router.get("/rules/recent")
async def get_recent_alerts(store: Annotated[AlertStore, Depends(get_store)]) -> object:
    return store.get_recent_events(limit=100)
