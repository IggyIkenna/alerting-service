import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI

from alerting_service.api.routes.alerts import router as alerts_router
from alerting_service.api.routes.delivery_status import router as delivery_status_router
from alerting_service.api.routes.health import router as health_router
from alerting_service.api.routes.safety_ops import router as safety_ops_router
from alerting_service.api.routes.system_status import router as system_status_router
from alerting_service.auth import auth_cfg, verify_api_key
from alerting_service.config import AlertingSystemConfig
from alerting_service.gateway.manual_action_endpoint import router as manual_action_router
from alerting_service.subscribers.alert_subscriber import AlertSubscriber

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run the live AlertSubscriber pull-loop alongside the HTTP server.

    When ``run_subscriber_in_api`` is set (env ``RUN_SUBSCRIBER_IN_API=true``) the
    FastAPI app launches ``AlertSubscriber(...).run_until_stopped()`` as a background
    task — so a single Cloud Run service both serves ``$PORT`` (startup probe / health)
    AND consumes PubSub (``lifecycle-events-sub`` → DP_* / CONSOLIDATOR_DOWN →
    ``#data-pipeline-alerts``). This is the durable always-on subscriber that replaces
    the fragile batch-VM (stall-watchdog self-delete) deployment.
    SSOT: plans/active/issues/dp_event_pubsub_delivery_gap_2026_06_22.md.
    """
    config = AlertingSystemConfig()
    subscriber: AlertSubscriber | None = None
    task: asyncio.Task[None] | None = None
    if config.run_subscriber_in_api and not config.is_mock_mode():
        subscriber = AlertSubscriber(project_id=config.gcp_project_id)
        task = asyncio.create_task(subscriber.run_until_stopped())
        logger.info("alerting-service: live AlertSubscriber started in API lifespan")
    try:
        yield
    finally:
        if subscriber is not None:
            subscriber.stop()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


_env = auth_cfg.environment
app = FastAPI(
    title="alerting-service",
    version="1.0.0",
    docs_url="/docs" if _env != "production" else None,
    redoc_url="/redoc" if _env != "production" else None,
    openapi_url="/openapi.json" if _env != "production" else None,
    lifespan=_lifespan,
)

# --- Unauthenticated health endpoints ---
app.include_router(health_router)
app.include_router(system_status_router)

# --- Authenticated API routes (require API key) ---
_authenticated_router = APIRouter(dependencies=[Depends(verify_api_key)])
_authenticated_router.include_router(alerts_router)
_authenticated_router.include_router(delivery_status_router)
_authenticated_router.include_router(safety_ops_router)
_authenticated_router.include_router(manual_action_router)
app.include_router(_authenticated_router)
