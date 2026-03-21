"""
Main entry point for alerting-service.

Usage:
    python -m alerting_service --mode batch
    python -m alerting_service --mode live
"""

import argparse
import asyncio
import contextlib
import logging
import os
import uuid
from typing import cast

from unified_events_interface import log_event
from unified_internal_contracts import LifecycleEventType
from unified_trading_library import (
    GracefulShutdownHandler,
    LogLevel,
    PubSubEventSink,
    get_messaging_protocol,
    get_storage_protocol,
    setup_service_observability,
)

from .config import AlertingSystemConfig
from .subscribers.alert_subscriber import AlertSubscriber

logger = logging.getLogger(__name__)

# Global shutdown handler
_shutdown_handler: GracefulShutdownHandler | None = None


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="alerting-service",
        description="Alerting Service CLI",
    )
    parser.add_argument(
        "--mode",
        choices=["batch", "live"],
        required=True,
        help="Execution mode: batch for historical, live for real-time",
    )
    return parser


async def _run_subscriber_until_shutdown(
    subscriber: AlertSubscriber,
    shutdown_handler: GracefulShutdownHandler,
    poll_interval: float = 0.5,
) -> None:
    """Drive the subscriber stream and stop it when shutdown is requested.

    Polls shutdown_handler.is_shutdown_requested() every *poll_interval* seconds
    so that SIGTERM/SIGINT are honoured promptly without blocking the event loop.
    """
    subscriber_task = asyncio.create_task(subscriber.run_until_stopped())
    try:
        while not shutdown_handler.is_shutdown_requested():
            if subscriber_task.done():
                break
            await asyncio.sleep(poll_interval)
    finally:
        subscriber.stop()
        subscriber_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscriber_task


async def main() -> None:
    """Main service logic."""
    global _shutdown_handler

    # LOG_LEVEL env var validation (SSOT for log levels)
    _raw_log_level = os.environ.get("LOG_LEVEL", "INFO")  # config-bootstrap: before UCC init
    try:
        _log_level = LogLevel(_raw_log_level)
    except ValueError as err:
        valid = ", ".join(v.value for v in LogLevel)
        raise SystemExit(f"Invalid LOG_LEVEL={_raw_log_level!r}. Must be one of: {valid}") from err
    _level_map: dict[str, int] = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    logging.basicConfig(level=_level_map.get(_log_level.value, logging.INFO))

    parser = _build_parser()
    args = parser.parse_args()

    config = AlertingSystemConfig()

    # --- MOCK MODE: use pre-generated seed data ---
    if config.is_mock_mode():
        from alerting_service.engine.mock_data_provider import run_mock_pipeline

        logger.info("MOCK MODE: redirecting to mock pipeline")
        run_mock_pipeline()
        return

    # Setup unified observability (events + tracing + memory watchdog)
    sink = PubSubEventSink(
        project_id=config.gcp_project_id,
        topic=f"{config.service_name}-events",
        service_name=config.service_name,
    )
    setup_service_observability(
        "alerting-service",
        mode=cast(str, args.mode),
        sink=sink,
        enable_tracing=True,
        memory_threshold_pct=85.0,
    )

    # Initialize graceful shutdown handler (handles SIGTERM/SIGINT)
    _shutdown_handler = GracefulShutdownHandler()

    correlation_id = str(uuid.uuid4())
    _messaging = get_messaging_protocol(mode=cast(str, args.mode), service="alerting-service")
    _storage = get_storage_protocol(mode=cast(str, args.mode), service="alerting-service")
    logger.info("Alerting Service — transport: %s, storage: %s", _messaging, _storage)
    log_event(LifecycleEventType.STARTED, details={"correlation_id": correlation_id})

    subscriber = AlertSubscriber(project_id=config.gcp_project_id)

    try:
        await _run_subscriber_until_shutdown(subscriber, _shutdown_handler)

        log_event(LifecycleEventType.STOPPED, details={"correlation_id": correlation_id})
    except (OSError, ValueError, RuntimeError) as e:
        log_event(
            LifecycleEventType.FAILED,
            severity="ERROR",
            details={"error": str(e), "correlation_id": correlation_id},
        )
        raise
