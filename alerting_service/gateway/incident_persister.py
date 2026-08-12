"""Incident state persister — P0.11.

Writes IncidentEnvelope snapshots + AgentActionEvent rows to GCS as
append-only JSONL blobs under ``incidents/{YYYY-MM-DD}/{incident_key}/``.

Designed to be injected as the ``PersistCallback`` in ``IncidentStateMachine``
and as a direct writer for ``AgentActionEvent`` rows emitted by the router.

GCS layout (one blob per write — readers list-and-merge per incident_key)::

    {bucket}/incidents/{YYYY-MM-DD}/{incident_key}/envelope/{uuid}.jsonl
    {bucket}/incidents/{YYYY-MM-DD}/{incident_key}/actions/{uuid}.jsonl

Codex SSOT: ``codex/04-architecture/incident-gateway-state-machine.md`` §
"Incident Persister".
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import cast

from unified_api_contracts.incident import AgentActionEvent, IncidentEnvelope, IncidentState
from unified_trading_library import (
    StorageClient,
    UnifiedCloudConfig,
    get_storage_client,
    resolve_bucket_name,
)

_logger = logging.getLogger(__name__)

_INCIDENTS_PREFIX = "incidents"


def _date_partition(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%d")


def _envelope_blob(incident_key: str, date_str: str, blob_id: str) -> str:
    return f"{_INCIDENTS_PREFIX}/{date_str}/{incident_key}/envelope/{blob_id}.jsonl"


def _actions_blob(incident_key: str, date_str: str, blob_id: str) -> str:
    return f"{_INCIDENTS_PREFIX}/{date_str}/{incident_key}/actions/{blob_id}.jsonl"


class IncidentPersister:
    """Append-only JSONL writer for IncidentEnvelope and AgentActionEvent.

    Construct once per process. Inject ``persist_envelope`` as the
    ``IncidentStateMachine`` persist callback::

        persister = IncidentPersister()
        sm = IncidentStateMachine(persist=persister.persist_envelope)
    """

    def __init__(
        self,
        storage_client: StorageClient | None = None,
        bucket: str | None = None,
    ) -> None:
        config = UnifiedCloudConfig()
        self._client: StorageClient = storage_client or get_storage_client(
            project_id=config.gcp_project_id,
        )
        self._bucket = bucket or _resolve_bucket(config)

    def persist_envelope(
        self,
        envelope: IncidentEnvelope,
        previous_state: IncidentState,
    ) -> None:
        """Write one IncidentEnvelope snapshot JSONL blob per transition.

        Called by IncidentStateMachine after each successful state change.
        Failures are logged and swallowed (per StateMachine persist contract).
        """
        date_str = _date_partition(envelope.timestamp)
        blob_id = str(uuid.uuid4())
        blob = _envelope_blob(envelope.incident_key, date_str, blob_id)
        record: dict[str, object] = {
            **json.loads(envelope.model_dump_json()),
            "_previous_state": previous_state.value,
        }
        self._write(blob, record)

    def persist_action_event(self, event: AgentActionEvent) -> None:
        """Write one AgentActionEvent JSONL blob."""
        date_str = _date_partition(event.timestamp)
        blob_id = event.event_id
        blob = _actions_blob(event.parent_incident_key, date_str, blob_id)
        record = cast("dict[str, object]", json.loads(event.model_dump_json()))
        self._write(blob, record)

    def _write(self, blob_path: str, record: dict[str, object]) -> None:
        line = (json.dumps(record, default=str) + "\n").encode()
        try:
            self._client.upload_bytes(
                bucket=self._bucket,
                blob_path=blob_path,
                data=line,
                content_type="application/x-ndjson",
            )
        except Exception:
            _logger.exception("IncidentPersister write failed: blob=%s bucket=%s", blob_path, self._bucket)


def _resolve_bucket(config: UnifiedCloudConfig) -> str:
    try:
        return resolve_bucket_name(cloud="gcp", kind="audit-records")
    except Exception:
        return f"uts-audit-store-{config.gcp_project_id}"
