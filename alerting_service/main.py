"""
Main entry point for alerting-service.

Usage:
    python -m alerting_service --mode batch
    python -m alerting_service --mode live
"""

import argparse
import asyncio
import logging
from typing import cast

from unified_events_interface import log_event, setup_events
from unified_internal_contracts import LifecycleEventType
from unified_trading_library import GracefulShutdownHandler, PubSubEventSink

from .config import AlertingSystemConfig

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


async def main() -> None:
    """Main service logic."""
    global _shutdown_handler

    parser = _build_parser()
    args = parser.parse_args()

    config = AlertingSystemConfig()

    # Setup event logging
    sink = PubSubEventSink(project_id=config.gcp_project_id, topic_id=f"{config.service_name}-events")
    setup_events(
        service_name=config.service_name,
        mode=cast(str, args.mode),
        sink=sink,
    )

    # Initialize graceful shutdown handler (handles SIGTERM/SIGINT)
    _shutdown_handler = GracefulShutdownHandler()

    log_event(LifecycleEventType.STARTED)

    try:
        # TODO: Implement service logic
        pass

        log_event(LifecycleEventType.STOPPED)
    except (OSError, ValueError, RuntimeError) as e:
        log_event(LifecycleEventType.FAILED, severity="ERROR", details={"error": str(e)})
        raise


if __name__ == "__main__":
    asyncio.run(main())
