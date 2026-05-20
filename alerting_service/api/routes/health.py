from datetime import UTC, datetime
from typing import Any

from unified_trading_library import UnifiedCloudConfig, make_health_router

_cloud_cfg = UnifiedCloudConfig()


def _data_freshness() -> dict[str, Any]:
    """Return data freshness info for health endpoint.

    Reports the age of the most recent alert data processed by the service.
    Placeholder -- real implementation would check actual alert timestamps.
    """
    return {
        "last_processed_date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "stale": False,
    }


def _readiness_check() -> dict[str, str]:
    checks: dict[str, str] = {}
    try:
        cfg = UnifiedCloudConfig()
        _ = cfg.gcp_project_id
        checks["config"] = "ok"
    except (RuntimeError, ValueError, OSError) as e:
        checks["config"] = f"error: {e}"
    return checks


router = make_health_router(
    service_name="alerting-service",
    version="1.0.0",
    readiness_check=_readiness_check,
    data_freshness=_data_freshness,
    cloud_provider=_cloud_cfg.cloud_provider,
    mock_mode=_cloud_cfg.is_mock_mode(),
)
