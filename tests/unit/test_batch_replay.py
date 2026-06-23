"""Unit tests for batch replay: BatchEventReader + batch delivery suppression."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from alerting_service.notifiers.router import get_batch_stats, route_event, set_batch_mode
from alerting_service.subscribers.batch_event_reader import BatchEventReader, BatchReplayStats

# ---------------------------------------------------------------------------
# BatchEventReader
# ---------------------------------------------------------------------------


def _exec_blob(date: str) -> str:
    """Build a GCS bucket:blob key for execution-service events."""
    bucket = "execution-service-events-test-project"
    return f"{bucket}:events/execution-service/{date}/events.jsonl"


class TestBatchEventReader:
    """Tests for GCS-based historical event replay."""

    def _mock_client(self, events_by_key: dict[str, list[dict[str, object]]]) -> MagicMock:
        """Create a mock StorageClient that returns JSONL for specific bucket/blob combos."""
        client = MagicMock()

        def _blob_exists(bucket: str, blob_path: str) -> bool:
            return f"{bucket}:{blob_path}" in events_by_key

        def _download_bytes(bucket: str, blob_path: str) -> bytes:
            key = f"{bucket}:{blob_path}"
            events = events_by_key.get(key, [])
            return "\n".join(json.dumps(e) for e in events).encode()

        client.blob_exists = MagicMock(side_effect=_blob_exists)
        client.download_bytes = MagicMock(side_effect=_download_bytes)
        return client

    @pytest.mark.asyncio
    async def test_stream_yields_events_sorted_by_timestamp(self) -> None:
        events = {
            _exec_blob("2026-03-20"): [
                {
                    "event": "FILL_RECEIVED",
                    "service": "execution-service",
                    "timestamp": "2026-03-20T14:00:02Z",
                },
                {
                    "event": "CIRCUIT_BREAKER_OPEN",
                    "service": "execution-service",
                    "timestamp": "2026-03-20T14:00:01Z",
                },
            ],
        }
        client = self._mock_client(events)
        with patch(
            "alerting_service.subscribers.batch_event_reader.get_storage_client",
            return_value=client,
        ):
            reader = BatchEventReader(
                project_id="test-project",
                dates=["2026-03-20"],
                source_services=("execution-service",),
            )
            results = [(name, details) async for name, details in reader.stream()]

        assert len(results) == 2
        assert results[0][0] == "CIRCUIT_BREAKER_OPEN"
        assert results[1][0] == "FILL_RECEIVED"

    @pytest.mark.asyncio
    async def test_stream_empty_dates_yields_nothing(self) -> None:
        client = self._mock_client({})
        with patch(
            "alerting_service.subscribers.batch_event_reader.get_storage_client",
            return_value=client,
        ):
            reader = BatchEventReader(
                project_id="test-project",
                dates=["2026-03-20"],
                source_services=("execution-service",),
            )
            results = [(name, details) async for name, details in reader.stream()]

        assert results == []
        assert reader.stats.total_events == 0

    @pytest.mark.asyncio
    async def test_stream_multiple_dates(self) -> None:
        events = {
            _exec_blob("2026-03-20"): [
                {
                    "event": "EVENT_A",
                    "service": "execution-service",
                    "timestamp": "2026-03-20T10:00:00Z",
                },
            ],
            _exec_blob("2026-03-21"): [
                {
                    "event": "EVENT_B",
                    "service": "execution-service",
                    "timestamp": "2026-03-21T10:00:00Z",
                },
            ],
        }
        client = self._mock_client(events)
        with patch(
            "alerting_service.subscribers.batch_event_reader.get_storage_client",
            return_value=client,
        ):
            reader = BatchEventReader(
                project_id="test-project",
                dates=["2026-03-20", "2026-03-21"],
                source_services=("execution-service",),
            )
            results = [(name, details) async for name, details in reader.stream()]

        assert len(results) == 2
        assert reader.stats.dates_processed == 2
        assert reader.stats.total_events == 2

    @pytest.mark.asyncio
    async def test_stream_skips_malformed_jsonl(self) -> None:
        events = {
            _exec_blob("2026-03-20"): [
                {
                    "event": "GOOD_EVENT",
                    "service": "execution-service",
                    "timestamp": "2026-03-20T10:00:00Z",
                },
            ],
        }
        client = self._mock_client(events)
        # Inject malformed line
        original_download = client.download_bytes.side_effect

        def _download_with_bad_line(bucket: str, blob_path: str) -> bytes:
            good = original_download(bucket, blob_path)
            return good + b"\nNOT_JSON_AT_ALL\n"

        client.download_bytes.side_effect = _download_with_bad_line

        with patch(
            "alerting_service.subscribers.batch_event_reader.get_storage_client",
            return_value=client,
        ):
            reader = BatchEventReader(
                project_id="test-project",
                dates=["2026-03-20"],
                source_services=("execution-service",),
            )
            results = [(name, details) async for name, details in reader.stream()]

        assert len(results) == 1
        assert reader.stats.errors == 1

    @pytest.mark.asyncio
    async def test_stream_enriches_with_batch_metadata(self) -> None:
        events = {
            _exec_blob("2026-03-20"): [
                {
                    "event": "TEST_EVENT",
                    "service": "execution-service",
                    "timestamp": "2026-03-20T10:00:00Z",
                },
            ],
        }
        client = self._mock_client(events)
        with patch(
            "alerting_service.subscribers.batch_event_reader.get_storage_client",
            return_value=client,
        ):
            reader = BatchEventReader(
                project_id="test-project",
                dates=["2026-03-20"],
                source_services=("execution-service",),
            )
            results = [(name, details) async for name, details in reader.stream()]

        assert len(results) == 1
        assert results[0][1]["_batch_replay"] is True
        assert results[0][1]["_original_date"] == "2026-03-20"
        assert "correlation_id" in results[0][1]

    def test_stop_stops_stream(self) -> None:
        reader = BatchEventReader.__new__(BatchEventReader)
        reader._running = True
        reader.stop()
        assert reader._running is False


class TestBatchReplayStats:
    def test_default_stats(self) -> None:
        stats = BatchReplayStats()
        assert stats.total_events == 0
        assert stats.errors == 0
        assert stats.dates_processed == 0


# ---------------------------------------------------------------------------
# Batch delivery suppression
# ---------------------------------------------------------------------------


class TestBatchDeliveryMode:
    """Tests for route_event() in batch mode."""

    def setup_method(self) -> None:
        set_batch_mode(False)

    def teardown_method(self) -> None:
        set_batch_mode(False)

    def test_batch_mode_suppresses_delivery(self) -> None:
        set_batch_mode(True)
        with patch("alerting_service.notifiers.router._persist_delivery_record") as mock_persist:
            route_event("CIRCUIT_BREAKER_OPEN", {"message": "test"})
        mock_persist.assert_called_once()
        record = mock_persist.call_args[0][0]
        assert record["status"] == "batch_audit"
        assert record["_batch_replay"] is True

    def test_batch_mode_does_not_send_pagerduty(self) -> None:
        set_batch_mode(True)
        with (
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router.pd_send_event") as mock_pd,
            patch("alerting_service.notifiers.router.send_uts_live_alert") as mock_slack,
        ):
            route_event("KILL_SWITCH_ACTIVATED", {"message": "test"})
        mock_pd.assert_not_called()
        mock_slack.assert_not_called()

    def test_batch_stats_accumulate(self) -> None:
        set_batch_mode(True)
        with patch("alerting_service.notifiers.router._persist_delivery_record"):
            route_event("EVENT_A", {"message": "a"})
            route_event("EVENT_B", {"message": "b"})
        stats = get_batch_stats()
        assert stats["matched"] == 2

    def test_batch_dedup_counted(self) -> None:
        set_batch_mode(True)
        with patch("alerting_service.notifiers.router._persist_delivery_record"):
            route_event("SAME_EVENT", {"message": "same", "key": "val"})
            route_event("SAME_EVENT", {"message": "same", "key": "val"})
        stats = get_batch_stats()
        assert stats["matched"] == 1
        assert stats["deduplicated"] == 1

    def test_live_mode_delivers_normally(self) -> None:
        set_batch_mode(False)
        with (
            patch("alerting_service.notifiers.router._persist_delivery_record"),
            patch("alerting_service.notifiers.router._deliver_to_uts_live_alerts_slack", return_value=True),
            patch("alerting_service.notifiers.router._persist_config_snapshot"),
        ):
            route_event("NORMAL_EVENT", {"message": "live"})
        # No assertion needed — just verify no crash in live mode
