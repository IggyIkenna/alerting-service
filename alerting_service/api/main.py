import logging

from fastapi import APIRouter, Depends, FastAPI, Header, Request

from alerting_service.api.routes.alerts import router as alerts_router
from alerting_service.api.routes.delivery_status import router as delivery_status_router
from alerting_service.api.routes.health import router as health_router
from alerting_service.api.routes.system_status import router as system_status_router
from alerting_service.auth import auth_cfg, verify_api_key
from alerting_service.auth_s2s import verify_service_token

logger = logging.getLogger(__name__)


async def verify_auth(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_service_token: str | None = Header(default=None, alias="X-Service-Token"),
) -> None:
    """Accept either API key or S2S token for authentication.

    S2S token is checked first (inter-service calls).
    Falls back to API key validation (which respects DISABLE_AUTH for dev mode).
    """
    if x_service_token:
        await verify_service_token(x_service_token=x_service_token, request=request)
        return
    # Delegate to verify_api_key — handles DISABLE_AUTH internally
    await verify_api_key(api_key=x_api_key)


_env = auth_cfg.environment
app = FastAPI(
    title="Alerting System",
    version="1.0.0",
    docs_url="/docs" if _env != "production" else None,
    redoc_url="/redoc" if _env != "production" else None,
    openapi_url="/openapi.json" if _env != "production" else None,
)

# --- Unauthenticated health endpoints ---
app.include_router(health_router)
app.include_router(system_status_router)

# --- Authenticated API routes (require API key or S2S token) ---
_authenticated_router = APIRouter(dependencies=[Depends(verify_auth)])
_authenticated_router.include_router(alerts_router)
_authenticated_router.include_router(delivery_status_router)
app.include_router(_authenticated_router)
