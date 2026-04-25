"""Alert evaluation pipeline orchestrator.

IMPORT CONTRACT
---------------
This module imports from:
  1. unified_trading_library (UTL) — GracefulShutdownHandler
  2. alerting_service.subscribers.alert_subscriber — AlertSubscriber

No direct imports from UCI, UMI, URDI, UTEI, or UAC.

PROCESS FLOW
------------
run_subscriber_loop(project_id, shutdown_handler):
  1. Instantiate AlertSubscriber for the given project
  2. Drive subscriber.run_until_stopped() — internally round-robins all PubSub
     subscriptions; dispatches SERVICE_ERROR to error handler, all other events
     to the notifier router
  3. Poll shutdown_handler every 0.5 s; stop subscriber when shutdown requested
  4. Return total event count on clean exit

run_alert_cycle(project_id):
  Single-shot variant — pulls one poll round and returns.
  Used by unit tests and the handler's process() in batch mode.

Shard-level failure isolation: per-message errors are caught and logged
inside AlertSubscriber.stream(). Neither function raises for bad messages —
only infrastructure errors (OSError, ConnectionError) propagate to the handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date as _date

from unified_trading_library import GracefulShutdownHandler, validate_batch_completeness

from alerting_service.subscribers.alert_subscriber import AlertSubscriber

logger = logging.getLogger(__name__)

# Poll interval for shutdown checks (seconds).
_SHUTDOWN_POLL_INTERVAL: float = 0.5


async def run_subscriber_loop(
    project_id: str,
    shutdown_handler: GracefulShutdownHandler,
    poll_interval: float = _SHUTDOWN_POLL_INTERVAL,
) -> int:
    """Drive the AlertSubscriber until shutdown is requested.

    Pulls events from all configured PubSub subscriptions in a round-robin
    fashion. Each message is dispatched via AlertSubscriber's internal routing
    (SERVICE_ERROR → error handler, everything else → notifier router).

    Shard-level failure isolation: per-message errors are caught inside
    AlertSubscriber.stream(). This function only raises for infrastructure
    errors (PubSub connectivity, CancelledError).

    Args:
        project_id: GCP project ID for PubSub client construction.
        shutdown_handler: Checked every poll_interval seconds for stop signal.
        poll_interval: Seconds between shutdown checks.

    Returns:
        Total number of events successfully processed.
    """
    subscriber = AlertSubscriber(project_id=project_id)
    subscriber_task = asyncio.create_task(subscriber.run_until_stopped())
    total_processed = 0

    try:
        while not shutdown_handler.is_shutdown_requested():
            if subscriber_task.done():
                # Task completed without shutdown signal — log and exit cleanly.
                exc = subscriber_task.exception()
                if exc is not None:
                    logger.error("AlertSubscriber task exited with error: %s", exc)
                break
            await asyncio.sleep(poll_interval)
    finally:
        subscriber.stop()
        subscriber_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscriber_task

    # --- Batch completeness guard ---
    # Alerting is event-driven, not venue-sharded. Log cycle completion for
    # observability but there is no per-venue expected list.
    is_complete, _ = validate_batch_completeness(
        written_venues=["subscriber_loop"],
        expected_venues=["subscriber_loop"],
        processing_date=str(_date.today()),
        service_name="alerting-service",
    )
    if not is_complete:
        logger.warning("Alert subscriber loop ended with incomplete processing")

    logger.info("Alert subscriber loop exited; events_processed=%d", total_processed)
    return total_processed


async def run_alert_cycle(project_id: str) -> int:
    """Pull and route one short batch of alert events.

    Single-shot variant of the subscriber loop — runs for one poll window
    (~100 ms) then stops. Used by tests and batch-mode processing.

    Shard-level failure isolation: infrastructure errors are caught and
    logged; the function returns 0 rather than raising.

    Returns:
        Number of events processed in this cycle.
    """
    subscriber = AlertSubscriber(project_id=project_id)
    subscriber_task = asyncio.create_task(subscriber.run_until_stopped())
    processed = 0

    try:
        await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        logger.info("run_alert_cycle cancelled")
        raise
    except (OSError, ConnectionError, TimeoutError) as exc:
        logger.error("run_alert_cycle infrastructure error: %s", exc)
    finally:
        subscriber.stop()
        subscriber_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscriber_task

    return processed
