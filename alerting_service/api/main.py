import logging

from fastapi import APIRouter, Depends, FastAPI

from alerting_service.api.routes.alerts import router as alerts_router
from alerting_service.api.routes.delivery_status import router as delivery_status_router
from alerting_service.api.routes.health import router as health_router
from alerting_service.api.routes.system_status import router as system_status_router
from alerting_service.auth import auth_cfg, verify_api_key

logger = logging.getLogger(__name__)

_env = auth_cfg.environment
app = FastAPI(
    title="alerting-service",
    version="1.0.0",
    docs_url="/docs" if _env != "production" else None,
    redoc_url="/redoc" if _env != "production" else None,
    openapi_url="/openapi.json" if _env != "production" else None,
)

# --- Unauthenticated health endpoints ---
app.include_router(health_router)
app.include_router(system_status_router)

# --- Authenticated API routes (require API key) ---
_authenticated_router = APIRouter(dependencies=[Depends(verify_api_key)])
_authenticated_router.include_router(alerts_router)
_authenticated_router.include_router(delivery_status_router)
app.include_router(_authenticated_router)
