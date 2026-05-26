"""Unit tests for P0.11 IncidentPersister — append-only JSONL → GCS."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from unified_api_contracts.alerting import AlertSeverity
from unified_api_contracts.incident import (
    ActionProvenance,
    ActionStatus,
    ActionType,
    AgentActionEvent,
    IncidentEnvelope,
    IncidentState,
)

from alerting_service.gateway.incident_persister import (
    IncidentPersister,
    _actions_blob,
    _date_partition,
    _envelope_blob,
)

_NOW = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
_DATE = "2026-05-24"
_INCIDENT_KEY = "test-inc-001"


def _envelope(**overrides: object) -> IncidentEnvelope:
    defaults: dict[str, object] = {
        "event_id": "evt-001",
        "incident_key": _INCIDENT_KEY,
        "timestamp": _NOW,
        "state": IncidentState.DETECTED,
        "environment": "prod",
        "severity_hint": AlertSeverity.CRITICAL,
        "domain": "live_trading",
        "service": "execution-service",
        "component": "order-router",
        "problem_type": "OOM_DETECTED",
        "problem_summary": "OOM kill",
        "config_hash": "sha256:abc",
        "code_version": "v1.0.0",
        "human_audit_ack_required": False,
        "audit_ack_due_at": _NOW + timedelta(hours=6),
    }
    defaults.update(overrides)
    return IncidentEnvelope(**defaults)  # type: ignore[arg-type]


def _action_event() -> AgentActionEvent:
    return AgentActionEvent(
        event_id="action-uuid-001",
        parent_incident_key=_INCIDENT_KEY,
        timestamp=_NOW,
        agent_id="agent-recovery-controller",
        action_type=ActionType.CANCEL_OPEN_ORDERS,
        action_status=ActionStatus.STARTED,
        provenance=ActionProvenance.AUTOMATIC,
        runbook_id="RB-RISK-001",
        config_hash="sha256:abc",
        code_version="v1.0.0",
    )


def _mock_persister() -> tuple[IncidentPersister, MagicMock]:
    client = MagicMock()
    client.upload_bytes = MagicMock()
    persister = IncidentPersister(storage_client=client, bucket="test-bucket")
    return persister, client


class TestBlobPathHelpers:
    def test_date_partition_utc(self):
        assert _date_partition(_NOW) == _DATE

    def test_envelope_blob_format(self):
        blob = _envelope_blob("my-incident", "2026-05-24", "some-uuid")
        assert blob == "incidents/2026-05-24/my-incident/envelope/some-uuid.jsonl"

    def test_actions_blob_format(self):
        blob = _actions_blob("my-incident", "2026-05-24", "some-uuid")
        assert blob == "incidents/2026-05-24/my-incident/actions/some-uuid.jsonl"


class TestPersistEnvelope:
    def test_upload_called_once(self):
        persister, client = _mock_persister()
        env = _envelope()
        persister.persist_envelope(env, IncidentState.DETECTED)
        assert client.upload_bytes.call_count == 1

    def test_blob_path_contains_incident_key_and_date(self):
        persister, client = _mock_persister()
        env = _envelope()
        persister.persist_envelope(env, IncidentState.DETECTED)
        kwargs = client.upload_bytes.call_args[1]
        assert _INCIDENT_KEY in kwargs["blob_path"]
        assert _DATE in kwargs["blob_path"]
        assert "envelope" in kwargs["blob_path"]

    def test_payload_contains_state_and_previous_state(self):
        persister, client = _mock_persister()
        env = _envelope(state=IncidentState.AUTO_ACTION_STARTED)
        persister.persist_envelope(env, IncidentState.DETECTED)
        kwargs = client.upload_bytes.call_args[1]
        record = json.loads(kwargs["data"].decode())
        assert record["state"] == "AUTO_ACTION_STARTED"
        assert record["_previous_state"] == "DETECTED"

    def test_content_type_is_ndjson(self):
        persister, client = _mock_persister()
        persister.persist_envelope(_envelope(), IncidentState.DETECTED)
        kwargs = client.upload_bytes.call_args[1]
        assert kwargs["content_type"] == "application/x-ndjson"

    def test_upload_error_swallowed(self):
        persister, client = _mock_persister()
        client.upload_bytes.side_effect = RuntimeError("GCS unavailable")
        persister.persist_envelope(_envelope(), IncidentState.DETECTED)  # must not raise

    def test_bucket_passed_correctly(self):
        persister, client = _mock_persister()
        persister.persist_envelope(_envelope(), IncidentState.DETECTED)
        kwargs = client.upload_bytes.call_args[1]
        assert kwargs["bucket"] == "test-bucket"


class TestPersistActionEvent:
    def test_upload_called_once(self):
        persister, client = _mock_persister()
        persister.persist_action_event(_action_event())
        assert client.upload_bytes.call_count == 1

    def test_blob_path_uses_event_id(self):
        persister, client = _mock_persister()
        persister.persist_action_event(_action_event())
        kwargs = client.upload_bytes.call_args[1]
        assert "action-uuid-001" in kwargs["blob_path"]
        assert "actions" in kwargs["blob_path"]

    def test_payload_contains_action_type(self):
        persister, client = _mock_persister()
        persister.persist_action_event(_action_event())
        kwargs = client.upload_bytes.call_args[1]
        record = json.loads(kwargs["data"].decode())
        assert record["action_type"] == "cancel_open_orders"

    def test_upload_error_swallowed(self):
        persister, client = _mock_persister()
        client.upload_bytes.side_effect = OSError("network timeout")
        persister.persist_action_event(_action_event())  # must not raise

    def test_each_event_writes_separate_blob(self):
        persister, client = _mock_persister()
        event1 = _action_event()
        event2 = AgentActionEvent(
            event_id="action-uuid-002",
            parent_incident_key=_INCIDENT_KEY,
            timestamp=_NOW,
            agent_id="agent-recovery-controller",
            action_type=ActionType.CANCEL_OPEN_ORDERS,
            action_status=ActionStatus.SUCCEEDED,
            provenance=ActionProvenance.AUTOMATIC,
            runbook_id="RB-RISK-001",
            config_hash="sha256:abc",
            code_version="v1.0.0",
        )
        persister.persist_action_event(event1)
        persister.persist_action_event(event2)
        blobs = [c[1]["blob_path"] for c in client.upload_bytes.call_args_list]
        assert blobs[0] != blobs[1]
