"""GET /alerts/delivery-status/{alert_id} — query delivery history from GCS."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from alerting_service.config import AlertingSystemConfig
from alerting_service.persistence.storage_store import AlertStorageStore

router = APIRouter(tags=["delivery"])

logger = logging.getLogger(__name__)

storage_store: AlertStorageStore | None = None
_cfg = AlertingSystemConfig()


def get_delivery_store() -> AlertStorageStore:
    """Return a lazily-initialised singleton AlertStorageStore."""
    global storage_store
    if storage_store is None:
        storage_store = AlertStorageStore()
    return storage_store


@router.get("/alerts/delivery-status/{alert_id}")
def get_delivery_status(
    alert_id: str,
    store: Annotated[AlertStorageStore, Depends(get_delivery_store)],
) -> dict[str, object]:
    """Return delivery records for a given alert_id.

    Scans GCS history partitions (last 7 days) for matching records.

    Returns:
        JSON object with ``alert_id`` and a list of ``records``.
    """
    if _cfg.cloud_mock_mode:
        from alerting_service.api.routes.mock_state import get_store as get_mock_store

        all_records = get_mock_store().list("delivery_records")
        matching = [r for r in all_records if r.get("alert_id") == alert_id]
        if not matching:
            # Return sample records with the requested alert_id
            matching = [
                {"delivery_id": "del-mock-001", "alert_id": alert_id, "channel": "telegram", "status": "delivered", "delivered_at": "2026-03-14T12:00:01Z", "acknowledged": False},
                {"delivery_id": "del-mock-002", "alert_id": alert_id, "channel": "pagerduty", "status": "delivered", "delivered_at": "2026-03-14T12:00:02Z", "acknowledged": True},
            ]
        return {
            "alert_id": alert_id,
            "records": matching,
        }
    records = store.read_delivery_records(alert_id)
    return {
        "alert_id": alert_id,
        "records": records,
    }


@router.post("/alerts/delivery-status")
def create_delivery_record(
    record: dict[str, object],
) -> dict[str, object]:
    """Record a new delivery record. Persists in mock state when CLOUD_MOCK_MODE=true."""
    if _cfg.cloud_mock_mode:
        import uuid

        from alerting_service.api.routes.mock_state import get_store as get_mock_store

        if "delivery_id" not in record:
            record["delivery_id"] = f"del-{uuid.uuid4().hex[:6]}"
        record["id"] = record["delivery_id"]
        return get_mock_store().create("delivery_records", record)
    return record
