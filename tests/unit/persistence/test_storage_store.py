"""Unit tests for the GCS persistence store."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml

from alerting_service.persistence.storage_store import AlertStorageStore, QuietnessReport


@pytest.fixture
def mock_storage_client() -> MagicMock:
    """Create a mock StorageClient."""
    return MagicMock()


@pytest.fixture
def mock_config() -> MagicMock:
    """Patch UnifiedCloudConfig to provide a fixed project_id."""
    mock_cfg = MagicMock()
    mock_cfg.gcp_project_id = "test-project"
    with patch("alerting_service.persistence.storage_store.UnifiedCloudConfig", return_value=mock_cfg):
        yield mock_cfg


@pytest.fixture
def storage_store(mock_storage_client: MagicMock, mock_config: MagicMock) -> AlertStorageStore:
    """Create an AlertStorageStore with a mock storage client."""
    return AlertStorageStore(storage_client=mock_storage_client, bucket="test-bucket")


@pytest.fixture
def mock_log_event() -> MagicMock:
    """Suppress log_event calls."""
    with patch("alerting_service.persistence.storage_store.log_event") as mock:
        yield mock


class TestWriteAlertHistory:
    def test_writes_jsonl_to_correct_prefix(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        alert = {"alert_id": "a1", "severity": "WARNING", "message": "test"}
        storage_store.write_alert_history(alert)

        mock_storage_client.upload_bytes.assert_called_once()
        call_kwargs = mock_storage_client.upload_bytes.call_args.kwargs
        assert call_kwargs["bucket"] == "test-bucket"
        assert call_kwargs["blob_path"].startswith("alerting/history/date=")
        assert call_kwargs["blob_path"].endswith(".jsonl")
        assert call_kwargs["content_type"] == "application/x-ndjson"

    def test_data_is_valid_jsonl(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        alert: dict[str, object] = {"alert_id": "a1", "severity": "WARNING"}
        storage_store.write_alert_history(alert)

        call_kwargs = mock_storage_client.upload_bytes.call_args.kwargs
        data = call_kwargs["data"]
        assert isinstance(data, bytes)
        parsed = json.loads(data.decode("utf-8").strip())
        assert parsed["alert_id"] == "a1"
        assert parsed["severity"] == "WARNING"

    def test_logs_event_on_success(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        storage_store.write_alert_history({"alert_id": "a1"})
        mock_log_event.assert_called_once()
        assert mock_log_event.call_args[0][0] == "PERSISTENCE_COMPLETED"

    def test_does_not_raise_on_upload_failure(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        mock_storage_client.upload_bytes.side_effect = RuntimeError("GCS down")
        # Should not raise
        storage_store.write_alert_history({"alert_id": "a1"})


class TestWriteConfigSnapshot:
    def test_writes_yaml_to_configs_prefix(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        config: dict[str, object] = {"rules": [{"name": "rule1"}]}
        storage_store.write_config_snapshot(config)

        call_kwargs = mock_storage_client.upload_bytes.call_args.kwargs
        assert call_kwargs["blob_path"] == "alerting/configs/default_rules.yaml"
        assert call_kwargs["content_type"] == "application/x-yaml"

    def test_custom_name(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        storage_store.write_config_snapshot({"rules": []}, name="custom_config")
        call_kwargs = mock_storage_client.upload_bytes.call_args.kwargs
        assert call_kwargs["blob_path"] == "alerting/configs/custom_config.yaml"

    def test_data_is_valid_yaml(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        config: dict[str, object] = {"rules": [{"name": "rule1", "threshold": 0.9}]}
        storage_store.write_config_snapshot(config)

        call_kwargs = mock_storage_client.upload_bytes.call_args.kwargs
        parsed = yaml.safe_load(call_kwargs["data"].decode("utf-8"))
        assert parsed["rules"][0]["name"] == "rule1"

    def test_does_not_raise_on_upload_failure(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        mock_storage_client.upload_bytes.side_effect = RuntimeError("GCS down")
        storage_store.write_config_snapshot({"rules": []})


class TestCooldownState:
    def test_read_returns_empty_dict_when_blob_missing(
        self, storage_store: AlertStorageStore, mock_storage_client: MagicMock
    ) -> None:
        mock_storage_client.blob_exists.return_value = False
        result = storage_store.read_cooldown_state()
        assert result == {}

    def test_read_returns_parsed_json(self, storage_store: AlertStorageStore, mock_storage_client: MagicMock) -> None:
        state = {"rule-1": "2026-03-08T12:00:00+00:00"}
        mock_storage_client.blob_exists.return_value = True
        mock_storage_client.download_bytes.return_value = json.dumps(state).encode("utf-8")

        result = storage_store.read_cooldown_state()
        assert result == state

    def test_read_returns_empty_dict_on_error(
        self, storage_store: AlertStorageStore, mock_storage_client: MagicMock
    ) -> None:
        mock_storage_client.blob_exists.side_effect = RuntimeError("GCS down")
        result = storage_store.read_cooldown_state()
        assert result == {}

    def test_write_uploads_json(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        state: dict[str, object] = {"rule-1": "2026-03-08T12:00:00"}
        storage_store.write_cooldown_state(state)

        call_kwargs = mock_storage_client.upload_bytes.call_args.kwargs
        assert call_kwargs["blob_path"] == "alerting/state/cooldowns.json"
        assert call_kwargs["content_type"] == "application/json"
        parsed = json.loads(call_kwargs["data"].decode("utf-8"))
        assert parsed == state

    def test_write_does_not_raise_on_failure(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        mock_storage_client.upload_bytes.side_effect = RuntimeError("GCS down")
        storage_store.write_cooldown_state({"rule-1": "2026-03-08"})


class TestWriteQuietnessReport:
    """Tests for write_quietness_report — mirrors write_alert_history pattern."""

    def test_writes_jsonl_to_correct_blob_path(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        """Blob path must be events/alerting-service/{date}/quietness-{run_id}/report.jsonl."""
        report = [
            QuietnessReport(
                alert_code="RISK_RULE_BLOCKED",
                fires=5,
                suppressed=2,
                fp_count=1,
                fp_rate=0.2,
                threshold=0.1,
            )
        ]
        storage_store.write_quietness_report("run-abc", report)

        mock_storage_client.upload_bytes.assert_called_once()
        call_kwargs = mock_storage_client.upload_bytes.call_args.kwargs
        assert call_kwargs["bucket"] == "test-bucket"
        blob_path: str = call_kwargs["blob_path"]
        assert blob_path.startswith("events/alerting-service/")
        assert "quietness-run-abc" in blob_path
        assert blob_path.endswith("/report.jsonl")
        assert call_kwargs["content_type"] == "application/x-ndjson"

    def test_data_is_valid_jsonl_with_all_report_fields(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        """Each QuietnessReport row must serialize as a JSONL line with all fields."""
        report = [
            QuietnessReport(
                alert_code="RISK_RULE_MONITOR_FIRED",
                fires=10,
                suppressed=3,
                fp_count=0,
                fp_rate=0.0,
                threshold=0.05,
            )
        ]
        storage_store.write_quietness_report("run-xyz", report)

        call_kwargs = mock_storage_client.upload_bytes.call_args.kwargs
        raw = call_kwargs["data"].decode("utf-8").strip()
        parsed = json.loads(raw)
        assert parsed["alert_code"] == "RISK_RULE_MONITOR_FIRED"
        assert parsed["fires"] == 10
        assert parsed["suppressed"] == 3
        assert parsed["fp_count"] == 0
        assert parsed["fp_rate"] == 0.0
        assert parsed["threshold"] == 0.05

    def test_multiple_rows_written_as_multi_line_jsonl(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        """Multiple QuietnessReport rows must produce one line per row."""
        report = [
            QuietnessReport(alert_code="CODE_A", fires=1, suppressed=0, fp_count=0, fp_rate=0.0, threshold=0.0),
            QuietnessReport(alert_code="CODE_B", fires=2, suppressed=1, fp_count=0, fp_rate=0.0, threshold=0.0),
        ]
        storage_store.write_quietness_report("run-multi", report)

        call_kwargs = mock_storage_client.upload_bytes.call_args.kwargs
        lines = [ln for ln in call_kwargs["data"].decode("utf-8").strip().splitlines() if ln]
        assert len(lines) == 2
        codes = {json.loads(ln)["alert_code"] for ln in lines}
        assert codes == {"CODE_A", "CODE_B"}

    def test_empty_report_writes_empty_bytes(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        """An empty report list must not raise and must write empty/newline-only bytes."""
        storage_store.write_quietness_report("run-empty", [])
        mock_storage_client.upload_bytes.assert_called_once()

    def test_logs_persistence_completed_event(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        report = [QuietnessReport(alert_code="*", fires=0, suppressed=0, fp_count=0, fp_rate=0.0, threshold=0.0)]
        storage_store.write_quietness_report("run-log", report)
        mock_log_event.assert_called_once()
        assert mock_log_event.call_args[0][0] == "PERSISTENCE_COMPLETED"

    def test_does_not_raise_on_upload_failure(
        self,
        storage_store: AlertStorageStore,
        mock_storage_client: MagicMock,
        mock_log_event: MagicMock,
    ) -> None:
        """Upload failures must be swallowed (same isolation as write_alert_history)."""
        mock_storage_client.upload_bytes.side_effect = RuntimeError("GCS down")
        report = [QuietnessReport(alert_code="*", fires=1, suppressed=0, fp_count=0, fp_rate=0.0, threshold=0.0)]
        # Must not raise
        storage_store.write_quietness_report("run-err", report)


class TestReadDeliveryRecords:
    """Tests for read_delivery_records method."""

    def test_returns_matching_records(self, storage_store: AlertStorageStore, mock_storage_client: MagicMock) -> None:
        record1 = {"alert_id": "abc123", "channel": "telegram", "status": "sent"}
        record2 = {"alert_id": "abc123", "channel": "pagerduty", "status": "sent"}
        record_other = {"alert_id": "other456", "channel": "telegram", "status": "sent"}

        # Simulate two blobs in the current date partition
        blob_data_1 = (json.dumps(record1) + "\n" + json.dumps(record_other) + "\n").encode("utf-8")
        blob_data_2 = (json.dumps(record2) + "\n").encode("utf-8")

        blob_meta_1 = MagicMock()
        blob_meta_1.name = "blob1.jsonl"
        blob_meta_2 = MagicMock()
        blob_meta_2.name = "blob2.jsonl"

        mock_storage_client.list_blobs.return_value = [blob_meta_1, blob_meta_2]
        mock_storage_client.download_bytes.side_effect = [blob_data_1, blob_data_2] * 7

        results = storage_store.read_delivery_records("abc123")
        # Should find record1 and record2 but not record_other
        matching_channels = {r["channel"] for r in results}
        assert "telegram" in matching_channels
        assert "pagerduty" in matching_channels
        assert all(r["alert_id"] == "abc123" for r in results)

    def test_returns_empty_list_when_no_blobs(
        self, storage_store: AlertStorageStore, mock_storage_client: MagicMock
    ) -> None:
        mock_storage_client.list_blobs.return_value = []
        results = storage_store.read_delivery_records("nonexistent")
        assert results == []

    def test_returns_empty_list_on_gcs_error(
        self, storage_store: AlertStorageStore, mock_storage_client: MagicMock
    ) -> None:
        mock_storage_client.list_blobs.side_effect = RuntimeError("GCS down")
        results = storage_store.read_delivery_records("abc123")
        assert results == []

    def test_scans_multiple_date_partitions(
        self, storage_store: AlertStorageStore, mock_storage_client: MagicMock
    ) -> None:
        mock_storage_client.list_blobs.return_value = []
        storage_store.read_delivery_records("abc123")

        # Should scan 7 date partitions
        assert mock_storage_client.list_blobs.call_count == 7
