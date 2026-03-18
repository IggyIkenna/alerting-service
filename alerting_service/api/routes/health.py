from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from unified_config_interface import UnifiedCloudConfig

router = APIRouter()

_cloud_cfg = UnifiedCloudConfig()


def _data_freshness() -> dict[str, object]:
    """Return data freshness info for health endpoint.

    Reports the age of the most recent alert data processed by the service.
    Placeholder -- real implementation would check actual alert timestamps.
    """
    return {
        "last_processed_date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "stale": False,
    }


@router.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "service": "alerting-system",
        "cloud_provider": _cloud_cfg.cloud_provider,
        "mock_mode": _cloud_cfg.is_mock_mode(),
        "data_freshness": _data_freshness(),
    }


@router.get("/readiness")
async def readiness() -> JSONResponse:
    """Readiness probe — checks config is loaded and service is ready."""
    checks: dict[str, str] = {}
    try:
        _cfg = UnifiedCloudConfig()
        _ = _cfg.gcp_project_id
        checks["config"] = "ok"
    except (RuntimeError, ValueError, OSError) as e:
        checks["config"] = f"error: {e}"
    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ready" if all_ok else "not_ready", "checks": checks},
    )
