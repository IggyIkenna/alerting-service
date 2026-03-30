"""alerting-service — FastAPI health API.

Exposes /health and /readiness endpoints via UTL make_health_router.
"""

from __future__ import annotations

from datetime import date

from fastapi import FastAPI
from unified_trading_library import make_health_router

from alerting_service.api.routes.delivery_status import router as delivery_router

_last_processed_date: date | None = None


def set_last_processed_date(d: date) -> None:
    global _last_processed_date
    _last_processed_date = d


def _data_freshness() -> dict[str, object]:
    if _last_processed_date is None:
        return {"last_processed_date": None, "stale": True}
    return {"last_processed_date": _last_processed_date.isoformat(), "stale": False}


def create_app() -> FastAPI:
    app = FastAPI(
        title="alerting-service",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )
    health_router = make_health_router(
        service_name="alerting-service",
        version="0.1.0",
        data_freshness=_data_freshness,
    )
    app.include_router(health_router)
    app.include_router(delivery_router)
    return app


app = create_app()
