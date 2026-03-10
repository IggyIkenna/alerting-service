"""GET /system-status — aggregated health of all services."""

from __future__ import annotations

from fastapi import APIRouter

from alerting_service.config import AlertingSystemConfig
from alerting_service.core.system_health_aggregator import get_system_health

router = APIRouter(tags=["system-status"])


@router.get("/system-status")
def system_status() -> dict[str, object]:
    """Return aggregated health for all services (30s TTL cache)."""
    cfg = AlertingSystemConfig()
    return get_system_health(cfg)
