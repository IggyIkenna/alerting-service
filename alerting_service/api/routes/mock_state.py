"""Stateful mock store for alerting-service.

Initializes MockStateStore with seed data from mock_data.py so that
POST/PUT/DELETE mutations are reflected in subsequent GET responses
when CLOUD_MOCK_MODE=true.
"""

from __future__ import annotations

from datetime import datetime

from unified_trading_library import MockStateStore

from alerting_service.api.routes.mock_data import MOCK_DELIVERY_RECORDS, MOCK_RECENT_ALERTS

_store = MockStateStore("alerting-service")


def _alerts_as_dicts() -> list[dict[str, object]]:
    """Convert AlertEvent pydantic models to dicts with 'id' field."""
    result: list[dict[str, object]] = []
    for alert in MOCK_RECENT_ALERTS:
        d: dict[str, object] = alert.model_dump()
        d["id"] = d.get("alert_id", "")  # noqa: qg-empty-fallback
        # Convert datetime to string for JSON serialization
        triggered = d.get("triggered_at")
        if isinstance(triggered, datetime):
            d["triggered_at"] = triggered.isoformat()
        result.append(d)
    return result


def _delivery_records_as_dicts() -> list[dict[str, object]]:
    """Convert delivery records to dicts with 'id' field."""
    result: list[dict[str, object]] = []
    for record in MOCK_DELIVERY_RECORDS:
        d: dict[str, object] = dict(record)
        d["id"] = str(d.get("delivery_id", ""))
        result.append(d)
    return result


_store.seed("alerts", _alerts_as_dicts())
_store.seed("delivery_records", _delivery_records_as_dicts())


def get_store() -> MockStateStore:
    """Return the module-level MockStateStore singleton."""
    return _store
