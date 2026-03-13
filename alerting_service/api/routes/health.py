from fastapi import APIRouter
from fastapi.responses import JSONResponse
from unified_config_interface import UnifiedCloudConfig

router = APIRouter()

_cloud_cfg = UnifiedCloudConfig()


@router.get("/health")
async def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "service": "alerting-system",
        "cloud_provider": _cloud_cfg.cloud_provider,
        "mock_mode": _cloud_cfg.cloud_mock_mode,
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
