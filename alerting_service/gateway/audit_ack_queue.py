"""Audit-ack queue — incidents requiring human audit-ack within their SLA window.

In-memory implementation for Tier-2 scaffold; production deployment swaps in
Redis Streams (configurable per ``AuditAckQueue.__init__``). The queue is
durable across process restarts via the underlying GCS incident_persister
output — on cold start, the gateway rehydrates the queue from
``gs://<audit-store>/incidents/{today}/*/envelope.json`` snapshots.

Codex SSOT: ``codex/04-architecture/incident-gateway-state-machine.md``
§ "Audit-ack queue" +
``codex/15-runbooks/alerting/audit-acknowledgement-flow.md``.
"""

from __future__ import annotations

import heapq
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

from unified_api_contracts.incident import IncidentEnvelope

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditAckQueueEntry:
    """One row in the audit-ack queue."""

    incident_key: str
    audit_ack_due_at: datetime
    """SLA deadline — tz-aware UTC."""
    severity: str
    """AlertSeverity.value at incident time."""


@dataclass(order=True)
class _PrioritizedEntry:
    """Internal heap entry — orders by due_at ascending (earliest first).

    ``order=True`` makes ``<`` compare on ``sort_key`` alone (``entry`` is
    ``compare=False``), which heapq requires once the heap holds 2+ items.
    """

    sort_key: float  # POSIX seconds since epoch
    entry: AuditAckQueueEntry = field(compare=False)


class AuditAckQueue:
    """Sorted queue of incidents pending human audit-ack.

    Thread-safe. Backed by a min-heap on ``audit_ack_due_at`` for O(log N)
    insert + O(1) peek of earliest-due. The ``due_soon()`` method returns
    entries whose deadline has passed (for the ack-escalation cron).

    The escalation cron (``alerting_service/gateway/ack_escalation.py``,
    scheduled separately) polls ``due_soon()`` every 30s; for each breaching
    entry it triggers the next step in the SLA ladder (secondary PagerDuty →
    founder Twilio → physical pager).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._heap: list[_PrioritizedEntry] = []
        self._by_key: dict[str, AuditAckQueueEntry] = {}

    def enqueue(self, envelope: IncidentEnvelope) -> AuditAckQueueEntry | None:
        """Insert if the envelope requires human audit-ack + has a deadline set.

        Returns the queue entry inserted, or None if the envelope was skipped
        (already in queue OR no deadline OR ack already received).
        """
        if envelope.audit_ack_due_at is None:
            return None
        if envelope.audit_acked_at is not None:
            return None
        with self._lock:
            if envelope.incident_key in self._by_key:
                # Already queued — idempotent re-enqueue is a no-op.
                return None
            entry = AuditAckQueueEntry(
                incident_key=envelope.incident_key,
                audit_ack_due_at=envelope.audit_ack_due_at,
                severity=envelope.severity_hint.value,
            )
            heapq.heappush(
                self._heap,
                _PrioritizedEntry(
                    sort_key=envelope.audit_ack_due_at.timestamp(),
                    entry=entry,
                ),
            )
            self._by_key[envelope.incident_key] = entry
            _logger.info(
                "Audit-ack queue: enqueued incident_key=%s due_at=%s severity=%s",
                envelope.incident_key,
                envelope.audit_ack_due_at.isoformat(),
                envelope.severity_hint.value,
            )
            return entry

    def dequeue(self, incident_key: str) -> bool:
        """Remove an entry (e.g. after operator audit-acks). Returns True if removed."""
        with self._lock:
            if incident_key not in self._by_key:
                return False
            del self._by_key[incident_key]
            # Heap still has the stale entry; ``peek`` + ``due_soon`` will
            # skip it via the by_key check.
            return True

    def due_soon(self, now: datetime) -> Iterator[AuditAckQueueEntry]:
        """Yield entries whose audit_ack_due_at <= now.

        Lazy generator — caller is the ack-escalation cron. Stale heap entries
        (where the incident was already acked + dequeued) are filtered out.
        """
        with self._lock:
            while self._heap:
                top = self._heap[0]
                if top.sort_key > now.timestamp():
                    return
                heapq.heappop(self._heap)
                entry = self._by_key.get(top.entry.incident_key)
                if entry is None:
                    # Already dequeued.
                    continue
                # Pop from by_key too — caller is responsible for re-enqueuing
                # if they want a retry (e.g. for the next escalation step).
                del self._by_key[top.entry.incident_key]
                yield entry

    def peek_due_at(self) -> datetime | None:
        """Return the earliest deadline currently in the queue, or None if empty."""
        with self._lock:
            while self._heap:
                top = self._heap[0]
                if top.entry.incident_key in self._by_key:
                    return top.entry.audit_ack_due_at
                # Stale; skip.
                heapq.heappop(self._heap)
            return None

    def pending_keys(self) -> list[str]:
        """Snapshot of incident_keys currently awaiting audit-ack (unordered)."""
        with self._lock:
            return list(self._by_key.keys())

    def size(self) -> int:
        with self._lock:
            return len(self._by_key)
