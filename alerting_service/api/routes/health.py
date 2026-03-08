from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "alerting-system"}


@router.get("/readiness")
async def readiness() -> JSONResponse:
    """Readiness probe — checks config is loaded and service is ready."""
    checks: dict[str, str] = {}
    try:
        from unified_config_interface import UnifiedCloudConfig

        _cfg = UnifiedCloudConfig()
        _ = _cfg.gcp_project_id
        checks["config"] = "ok"
    except Exception as e:
        checks["config"] = f"error: {e}"
    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ready" if all_ok else "not_ready", "checks": checks},
    )
