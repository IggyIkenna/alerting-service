import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI
from unified_trading_library import (
    LogLevel,
    UnifiedCloudConfig,
    create_api_auth,
    setup_cloud_logging,
)

from alerting_service.api.routes.alerts import router as alerts_router
from alerting_service.api.routes.delivery_status import router as delivery_status_router
from alerting_service.api.routes.health import router as health_router
from alerting_service.api.routes.safety_ops import router as safety_ops_router
from alerting_service.api.routes.system_status import router as system_status_router
from alerting_service.config import AlertingSystemConfig
from alerting_service.gateway.manual_action_endpoint import router as manual_action_router
from alerting_service.subscribers.alert_subscriber import AlertSubscriber


def _configure_stdout_logging() -> None:
    """Route the root logger to Cloud-Logging-structured JSON (flushed) so Cloud Run
    captures + correctly severity-tags app + route logs.

    The Cloud Run / uvicorn entrypoint (``uvicorn alerting_service.api.main:app``)
    NEVER runs the CLI ``main.py`` ``ServiceBootstrap`` logging setup — so without this
    call the root logger has no handler and ``logger.info(...)`` records (the consume +
    route + webhook-POST trace) are dropped (Python's lastResort handler only emits
    WARNING+).

    Root cause of the persisted "ZERO app logs in Cloud Logging" gap (2026-07-28,
    superseding the 2026-06-23 P2 fix): a per-record-flushing handler alone is
    necessary but NOT sufficient. Cloud Run's log agent assigns **plain-text**
    (non-JSON) stdout/stderr lines Cloud Logging severity ``DEFAULT`` (0) regardless of
    the Python log level — it does not parse ``%(levelname)s`` out of free text. The
    project's ``_Default`` log sink carries a ``debug-filter`` exclusion
    (``severity <= "DEBUG" AND NOT resource.type="cloud_run_job"``) that therefore drops
    EVERY plain-text line from a Cloud Run **service** (the exclusion only carves out
    **jobs**) before it ever reaches Cloud Logging — confirmed via
    ``gcloud logging read`` returning zero ``run.googleapis.com/stdout`` or ``/stderr``
    entries for ``dp-alerting-subscriber`` at ANY severity across 30 days, while a
    sibling Cloud Run JOB's plain-text DEFAULT-severity lines were present and
    unfiltered. Reusing UTL's ``CloudRunJSONFormatter`` (``severity`` set from
    ``record.levelname``) makes Cloud Run's agent honour the real Python level instead
    of defaulting to DEFAULT/0, so INFO+ lines now clear the exclusion without touching
    the project-wide sink policy (a shared, cost-sensitive resource — see
    ``prd-gcs-data-access-exclusion`` on the same sink). Mirrors UTL's
    ``setup_cloud_logging`` (identical helper already used by
    ``client-reporting-api``'s CLI entrypoint); reused here instead of re-derived.
    SSOT: plans/active/issues/dp_event_pubsub_delivery_gap_2026_06_22.md.
    """
    try:
        level_name = LogLevel(AlertingSystemConfig().log_level).value
    except ValueError:
        level_name = "INFO"
    setup_cloud_logging(log_level=level_name, json_format=True)


_configure_stdout_logging()

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


_env = UnifiedCloudConfig().environment
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
_api_auth = create_api_auth("alerting-service")
_authenticated_router = APIRouter(dependencies=[Depends(_api_auth)])
_authenticated_router.include_router(alerts_router)
_authenticated_router.include_router(delivery_status_router)
_authenticated_router.include_router(safety_ops_router)
_authenticated_router.include_router(manual_action_router)
app.include_router(_authenticated_router)
