"""Alert event subscriber for alerting-service.

Subscribes to the following PubSub topics via get_queue_client():
  - risk_alerts_circuit_breaker_triggers
  - balance_discrepancy_alerts
  - order_rejection_spikes

Also processes coordination events emitted by execution-service:
  - KILL_SWITCH_ACTIVATED
  - CIRCUIT_BREAKER_OPEN

On each message the event name is extracted and forwarded to route_event()
which dispatches to PagerDuty and/or Slack according to routing rules.

No direct google.cloud.pubsub_v1 imports — all PubSub access goes
through get_queue_client() from unified_cloud_interface.

Lifecycle event taxonomy (common events — alerting-service is a router, not a pipeline):
  SERVICE_EVENT: VALIDATION_STARTED
  SERVICE_EVENT: VALIDATION_COMPLETED
  SERVICE_EVENT: VALIDATION_FAILED
  SERVICE_EVENT: DATA_INGESTION_STARTED
  SERVICE_EVENT: DATA_INGESTION_COMPLETED
  SERVICE_EVENT: PROCESSING_STARTED
  SERVICE_EVENT: PROCESSING_COMPLETED
  SERVICE_EVENT: PERSISTENCE_STARTED
  SERVICE_EVENT: PERSISTENCE_COMPLETED
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import cast

from unified_cloud_interface import QueueClient, get_queue_client
from unified_events_interface import log_event

from ..metrics import PROCESSING_LATENCY, RECORDS_PROCESSED
from ..notifiers.router import route_event

logger = logging.getLogger(__name__)

# PubSub subscriptions to pull from.
_ALERT_SUBSCRIPTIONS: tuple[str, ...] = (
    "risk_alerts_circuit_breaker_triggers",
    "balance_discrepancy_alerts",
    "order_rejection_spikes",
)

# Coordination events from execution-service that must also be routed.
_COORDINATION_EVENTS: frozenset[str] = frozenset({"KILL_SWITCH_ACTIVATED", "CIRCUIT_BREAKER_OPEN"})


def _extract_event_name(payload: dict[str, object]) -> str:
    """Extract the canonical event_name from a deserialized PubSub payload.

    Looks for ``event_name``, ``event_type``, or ``type`` keys in that order.
    Falls back to ``"UNKNOWN_EVENT"`` when none are present.
    """
    for key in ("event_name", "event_type", "type"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "UNKNOWN_EVENT"


def _deserialize_message(data: bytes) -> tuple[str, dict[str, object]]:
    """Deserialize raw PubSub bytes into (event_name, details).

    Returns ("MALFORMED_EVENT", {}) on decode/parse failure so the caller
    can still log the anomaly without crashing the subscriber loop.
    """
    try:
        payload: dict[str, object] = cast(dict[str, object], json.loads(data.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Failed to decode alert message payload: %s", exc)
        return "MALFORMED_EVENT", {}

    event_name = _extract_event_name(payload)
    return event_name, payload


class AlertSubscriber:
    """Pulls alert events from multiple PubSub subscriptions and routes them.

    Uses get_queue_client() from unified_cloud_interface — no direct PubSub SDK.

    Each subscription is polled in a round-robin fashion inside a single async
    loop so that all topics share one event loop iteration.

    Usage::

        subscriber = AlertSubscriber(project_id="my-project")
        async for event_name, details in subscriber.stream():
            # route_event is called internally; this exposes the pair
            # for observability / testing hooks if needed.
            pass

        # Or simply run until shutdown:
        await subscriber.run_until_stopped()
    """

    def __init__(
        self,
        project_id: str,
        subscriptions: tuple[str, ...] = _ALERT_SUBSCRIPTIONS,
        poll_timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self._project_id = project_id
        self._subscriptions = subscriptions
        self._poll_timeout = poll_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._client: QueueClient = get_queue_client(project_id=project_id)
        self._running = False
        logger.info(
            "AlertSubscriber initialized: subscriptions=%s, project=%s",
            self._subscriptions,
            self._project_id,
        )

    def stop(self) -> None:
        """Signal the stream loop to stop after the current poll cycle."""
        self._running = False

    def _process_message(
        self,
        data: bytes,
        attrs: dict[str, str],
        subscription: str,
    ) -> tuple[str, dict[str, object]]:
        """Deserialize one PubSub message and enrich it with tracing metadata.

        Returns (event_name, enriched_details) ready to route and yield.
        """
        try:
            raw: dict[str, object] = cast(dict[str, object], json.loads(data.decode("utf-8")))
            correlation_id = str(raw.get("correlation_id") or uuid.uuid4())
        except (json.JSONDecodeError, UnicodeDecodeError):
            correlation_id = str(uuid.uuid4())

        event_name, details = _deserialize_message(data)
        enriched: dict[str, object] = {
            **details,
            "correlation_id": correlation_id,
            "source": attrs.get("source", subscription),
        }
        log_event(
            "ALERT_RECEIVED",
            details={
                "event_name": event_name,
                "subscription": subscription,
                "correlation_id": correlation_id,
            },
        )
        return event_name, enriched

    async def stream(self) -> AsyncIterator[tuple[str, dict[str, object]]]:
        """Yield (event_name, details) pairs from all subscribed topics.

        Calls route_event() for each message before yielding, so callers
        receive the pair only after routing has been attempted.

        Skips malformed messages with a warning; never crashes the loop.
        """
        self._running = True
        logger.info("Starting alert subscriber stream: %s", self._subscriptions)
        loop = asyncio.get_event_loop()
        try:
            while self._running:
                for subscription in self._subscriptions:
                    if not self._running:
                        break
                    batch: list[tuple[bytes, dict[str, str]]] = await loop.run_in_executor(
                        None,
                        lambda sub=subscription: self._client.subscribe_once(
                            sub, timeout=self._poll_timeout
                        ),
                    )
                    for data, attrs in batch:
                        _start = time.perf_counter()
                        try:
                            event_name, enriched = self._process_message(data, attrs, subscription)
                            route_event(event_name, enriched)
                            RECORDS_PROCESSED.labels(status="success").inc()
                        except Exception:
                            RECORDS_PROCESSED.labels(status="error").inc()
                            raise
                        finally:
                            PROCESSING_LATENCY.observe(time.perf_counter() - _start)
                        yield event_name, enriched

                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.info("AlertSubscriber stream cancelled")
            raise

    async def run_until_stopped(self) -> None:
        """Drive the stream loop until stop() is called or the task is cancelled.

        Consumes all yielded pairs; side-effects happen inside stream().
        """
        async for _event_name, _details in self.stream():
            pass
