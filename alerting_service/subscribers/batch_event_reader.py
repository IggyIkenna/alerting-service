"""Batch event reader for alerting-service.

Reads historical lifecycle events from GCS event logs and yields them
chronologically through the same interface as AlertSubscriber.stream().

Event log format (written by GcsEventSink / CompositeEventSink):
  gs://{bucket}/events/{service_name}/{YYYY-MM-DD}/events.jsonl

Each JSONL line:
  {"event": "CIRCUIT_BREAKER_OPEN", "service": "execution-service",
   "timestamp": "2026-03-20T14:23:01Z", "metadata": {...}}

Usage:
  reader = BatchEventReader(project_id="central-element-323112",
                            dates=["2026-03-20", "2026-03-21"])
  async for event_name, details in reader.stream():
      route_event(event_name, details)
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from unified_trading_library import StorageClient, get_storage_client
from unified_trading_library import log_event

logger = logging.getLogger(__name__)

# Services whose event logs we scan for batch replay.
# Each service writes to: events/{service_name}/{date}/events.jsonl
# in its own events bucket: {service_name}-events-{project_id}
# OR in a shared bucket. We try both conventions.
_EVENT_SOURCE_SERVICES: tuple[str, ...] = (
    "execution-service",
    "risk-and-exposure-service",
    "strategy-service",
    "position-balance-monitor-service",
    "instruments-service",
    "market-tick-data-service",
    "market-data-processing-service",
    "features-delta-one-service",
    "features-volatility-service",
    "features-onchain-service",
    "features-sports-service",
    "ml-training-service",
    "ml-inference-service",
    "pnl-attribution-service",
    "alerting-service",
)

# Bucket naming conventions to try (first match wins)
_BUCKET_PATTERNS: tuple[str, ...] = (
    "{service}-events-{project_id}",
    "{service_underscored}-events-{project_id}",
    "alerting-service-{project_id}",  # fallback: alerting's own bucket
)


@dataclass
class BatchReplayStats:
    """Accumulated statistics for a batch replay run."""

    total_events: int = 0
    events_by_service: dict[str, int] = field(default_factory=dict)
    services_scanned: int = 0
    services_with_data: int = 0
    services_missing: int = 0
    dates_processed: int = 0
    errors: int = 0


class BatchEventReader:
    """Reads historical events from GCS for batch alerting replay.

    Same stream() interface as AlertSubscriber so main.py can swap them
    based on --mode without changing downstream routing logic.
    """

    def __init__(
        self,
        project_id: str,
        dates: list[str],
        source_services: tuple[str, ...] = _EVENT_SOURCE_SERVICES,
    ) -> None:
        self._project_id = project_id
        self._dates = sorted(dates)
        self._source_services = source_services
        self._client: StorageClient = get_storage_client(provider="gcp", project_id=project_id)
        self._stats = BatchReplayStats()
        self._running = False

    @property
    def stats(self) -> BatchReplayStats:
        return self._stats

    def stop(self) -> None:
        """Signal the stream to stop after current date."""
        self._running = False

    async def stream(self) -> AsyncIterator[tuple[str, dict[str, object]]]:
        """Yield (event_name, details) pairs from GCS event logs.

        Reads all services' event logs for each date, sorts by timestamp,
        and yields chronologically. Same interface as AlertSubscriber.stream().
        """
        self._running = True
        log_event(
            "BATCH_REPLAY_STARTED",
            details={
                "dates": self._dates,
                "services": list(self._source_services),
                "date_count": len(self._dates),
            },
        )

        for date in self._dates:
            if not self._running:
                break
            self._stats.dates_processed += 1
            events = self._read_all_services_for_date(date)
            events.sort(key=lambda e: str(e.get("timestamp", "")))

            for event in events:
                if not self._running:
                    break
                event_name = str(event.get("event", event.get("event_name", "UNKNOWN_EVENT")))
                correlation_id = str(event.get("correlation_id", uuid.uuid4()))
                enriched: dict[str, object] = {
                    **event,
                    "correlation_id": correlation_id,
                    "source": str(event.get("service", "unknown")),
                    "_batch_replay": True,
                    "_original_date": date,
                }
                self._stats.total_events += 1
                svc = str(event.get("service", "unknown"))
                self._stats.events_by_service[svc] = self._stats.events_by_service.get(svc, 0) + 1
                yield event_name, enriched

        log_event(
            "BATCH_REPLAY_COMPLETED",
            details={
                "total_events": self._stats.total_events,
                "dates_processed": self._stats.dates_processed,
                "services_with_data": self._stats.services_with_data,
            },
        )

    def _read_all_services_for_date(self, date: str) -> list[dict[str, object]]:
        """Read event logs from all source services for a single date."""
        all_events: list[dict[str, object]] = []
        for service in self._source_services:
            self._stats.services_scanned += 1
            events = self._read_service_events(service, date)
            if events:
                all_events.extend(events)
                self._stats.services_with_data += 1
            else:
                self._stats.services_missing += 1
        return all_events

    def _read_service_events(self, service: str, date: str) -> list[dict[str, object]]:
        """Read JSONL events for one service on one date. Returns [] on failure."""
        blob_path = f"events/{service}/{date}/events.jsonl"

        for pattern in _BUCKET_PATTERNS:
            bucket = pattern.format(
                service=service,
                service_underscored=service.replace("-", "_"),
                project_id=self._project_id,
            )
            try:
                if not self._client.blob_exists(bucket=bucket, blob_path=blob_path):
                    continue
                raw = self._client.download_bytes(bucket=bucket, blob_path=blob_path)
                return self._parse_jsonl(raw.decode("utf-8"), service, date)
            except Exception:
                continue

        return []

    def _parse_jsonl(self, text: str, service: str, date: str) -> list[dict[str, object]]:
        """Parse JSONL text into event dicts. Skips malformed lines."""
        events: list[dict[str, object]] = []
        for line_num, line in enumerate(text.strip().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                record: dict[str, object] = json.loads(line)
                if "service" not in record:
                    record["service"] = service
                events.append(record)
            except json.JSONDecodeError:
                logger.warning(
                    "Malformed JSONL at %s/%s line %d, skipping",
                    service,
                    date,
                    line_num,
                )
                self._stats.errors += 1
        return events
