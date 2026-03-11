"""GET /system-status — aggregated health of all services."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from unified_events_interface import log_event

from alerting_service.config import AlertingSystemConfig
from alerting_service.core.system_health_aggregator import get_system_health

router = APIRouter(tags=["system-status"])


@router.get("/system-status")
def system_status() -> dict[str, object]:
    """Return aggregated health for all services (30s TTL cache)."""
    cfg = AlertingSystemConfig()
    log_event(
        "CONFIG_CHANGED",
        severity="INFO",
        details={
            "service": "alerting-service",
            "config_keys_updated": "AlertingSystemConfig",
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )
    return get_system_health(cfg)
