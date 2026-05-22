"""
Main entry point for alerting-service.

Usage:
    python -m alerting_service --mode batch
    python -m alerting_service --mode live
"""

import argparse
import logging
import uuid
from datetime import datetime as dt
from datetime import timedelta
from typing import cast

from unified_api_contracts.internal import LifecycleEventType  # noqa: qg-deep-import
from unified_trading_library import (
    GracefulShutdownHandler,
    LogLevel,
    PubSubEventSink,
    get_messaging_protocol,
    get_storage_protocol,
    log_event,
    setup_service_observability,
)

from .config import AlertingSystemConfig
from .engine.mock_data_provider import run_mock_pipeline
from .engine.orchestrator import run_subscriber_loop
from .notifiers.router import get_batch_stats, set_batch_mode
from .subscribers.alert_subscriber import AlertSubscriber
from .subscribers.batch_event_reader import BatchEventReader

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
        help="Execution mode: batch for historical replay, live for real-time",
    )
    parser.add_argument(
        "--date",
        help="Batch replay start date (YYYY-MM-DD). Required when --mode batch.",
    )
    parser.add_argument(
        "--end-date",
        help="Batch replay end date (YYYY-MM-DD). Defaults to --date for single-day replay.",
    )
    return parser


def _build_date_range(start: str, end: str) -> list[str]:
    """Build inclusive list of YYYY-MM-DD strings from start to end."""
    start_d = dt.strptime(start, "%Y-%m-%d")
    end_d = dt.strptime(end, "%Y-%m-%d")
    dates: list[str] = []
    current = start_d
    while current <= end_d:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


async def _run_batch_replay(
    args: argparse.Namespace,
    config: AlertingSystemConfig,
    shutdown_handler: GracefulShutdownHandler,
) -> None:
    """Run batch replay: read GCS event logs, route through rules, write audit records."""
    date_str: str = cast(str, args.date)
    if not date_str:
        raise SystemExit("--date is required when --mode batch")
    end_date_str: str = cast(str, getattr(args, "end_date", None) or date_str)
    dates = _build_date_range(date_str, end_date_str)
    logger.info("Batch replay: %s → %s (%d days)", date_str, end_date_str, len(dates))

    set_batch_mode(True)
    reader = BatchEventReader(project_id=config.gcp_project_id, dates=dates)

    async for event_name, enriched in reader.stream():
        if shutdown_handler.is_shutdown_requested():
            break
        AlertSubscriber.dispatch_event(event_name, enriched)

    set_batch_mode(False)

    stats = get_batch_stats()
    reader_stats = reader.stats
    logger.info("=" * 60)
    logger.info("Alerting Batch Replay Summary")
    logger.info("=" * 60)
    logger.info("Date range:     %s → %s", date_str, end_date_str)
    logger.info("Total events:   %d", reader_stats.total_events)
    logger.info("Events matched: %d (routing rule hit)", stats.get("matched", 0))
    would_deliver: dict[str, int] = cast(
        dict[str, int],
        stats.get("would_deliver", {}),  # noqa: qg-empty-fallback
    )
    for channel, count in sorted(would_deliver.items()):
        logger.info("  %-14s %d", f"{channel}:", count)
    logger.info("Deduplicated:   %d", stats.get("deduplicated", 0))
    logger.info("Services w/data:%d", reader_stats.services_with_data)
    logger.info("Errors:         %d", reader_stats.errors)
    logger.info("=" * 60)


async def main() -> None:
    """Main service logic."""
    global _shutdown_handler

    config = AlertingSystemConfig()

    try:
        _log_level = LogLevel(config.log_level)
    except ValueError as err:
        valid = ", ".join(v.value for v in LogLevel)
        raise SystemExit(
            f"Invalid LOG_LEVEL={config.log_level!r}. Must be one of: {valid}"
        ) from err
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

    # --- MOCK MODE: use pre-generated seed data ---
    if config.is_mock_mode():
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

    try:
        mode: str = cast(str, args.mode)
        if mode == "live":
            await run_subscriber_loop(config.gcp_project_id, _shutdown_handler)
        else:
            await _run_batch_replay(args, config, _shutdown_handler)

        log_event(LifecycleEventType.STOPPED, details={"correlation_id": correlation_id})
    except (OSError, ValueError, RuntimeError) as e:
        log_event(
            LifecycleEventType.FAILED,
            severity="ERROR",
            details={"error": str(e), "correlation_id": correlation_id},
        )
        raise
