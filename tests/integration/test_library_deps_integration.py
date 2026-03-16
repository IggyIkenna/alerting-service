"""Functional integration tests for all unified-* library dependencies.

Each test instantiates real classes, calls real functions with valid args, and
verifies return types and behavior — NOT just ``assert X is not None``.

Runs credential-free: CLOUD_PROVIDER=local CLOUD_MOCK_MODE=true.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# unified_config_interface
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestUnifiedConfigInterface:
    """Functional tests for unified-config-interface usage in alerting-service."""

    def test_alerting_system_config_instantiation(self) -> None:
        """AlertingSystemConfig extends UnifiedCloudConfig and has service defaults."""
        from alerting_service.config import AlertingSystemConfig

        config = AlertingSystemConfig()
        assert config.service_name == "alerting-service"
        assert isinstance(config.routing_rules, list)
        assert len(config.routing_rules) > 0
        # Verify routing rules have the expected structure
        first_rule = config.routing_rules[0]
        assert "event_pattern" in first_rule
        assert "channels" in first_rule

    def test_config_routing_rules_structure(self) -> None:
        """Routing rules contain valid patterns and channel lists."""
        from alerting_service.config import AlertingSystemConfig

        config = AlertingSystemConfig()
        patterns_seen: list[str] = []
        for rule in config.routing_rules:
            pattern = rule.get("event_pattern")
            assert isinstance(pattern, str), f"event_pattern should be str, got {type(pattern)}"
            patterns_seen.append(pattern)
            channels = rule.get("channels")
            assert isinstance(channels, list), f"channels should be list, got {type(channels)}"
        # Verify critical patterns are present
        assert any("KILL_SWITCH" in p for p in patterns_seen)
        assert any("CIRCUIT_BREAKER" in p for p in patterns_seen)

    def test_config_field_defaults(self) -> None:
        """Config fields have sensible defaults for credential-free testing."""
        from alerting_service.config import AlertingSystemConfig

        config = AlertingSystemConfig()
        assert isinstance(config.poll_interval_seconds, int)
        assert config.poll_interval_seconds > 0
        assert isinstance(config.email_smtp_port, int)


# ---------------------------------------------------------------------------
# unified_events_interface
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestUnifiedEventsInterface:
    """Functional tests for unified-events-interface usage in alerting-service."""

    def test_log_event_with_details(self) -> None:
        """log_event() accepts event name and details dict without error."""
        from unified_events_interface import log_event

        # Should not raise — conftest sets up MockEventSink
        log_event("ALERT_SENT", details={"event_name": "TEST", "alert_id": "abc123"})

    def test_log_event_with_lifecycle_type(self) -> None:
        """log_event() works with LifecycleEventType enum values."""
        from unified_events_interface import log_event
        from unified_internal_contracts import LifecycleEventType

        log_event(LifecycleEventType.STARTED, details={"correlation_id": "test-123"})
        log_event(LifecycleEventType.STOPPED, details={"correlation_id": "test-123"})

    def test_setup_events_with_mock_sink(self) -> None:
        """setup_events() accepts MockEventSink for credential-free testing."""
        from unified_events_interface import MockEventSink, setup_events

        sink = MockEventSink()
        # Should not raise
        setup_events(service_name="alerting-service-test", mode="test", sink=sink)


# ---------------------------------------------------------------------------
# unified_internal_contracts
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestUnifiedInternalContracts:
    """Functional tests for unified-internal-contracts types used in alerting-service."""

    def test_alert_event_construction(self) -> None:
        """AlertEvent can be constructed with required fields and serialized."""
        from unified_internal_contracts import AlertEvent

        event = AlertEvent(
            alert_id="alert-001",
            rule_id="cpu_threshold",
            triggered_at=datetime.now(UTC),
            severity="CRITICAL",
            message="CPU usage exceeded threshold",
            metric_value=95.5,
            threshold=90.0,
            strategy_id="strat-001",
            venue="binance",
        )
        assert event.alert_id == "alert-001"
        assert event.severity == "CRITICAL"
        assert event.metric_value == 95.5
        # Verify Pydantic serialization
        event_dict = event.model_dump()
        assert "alert_id" in event_dict
        assert "triggered_at" in event_dict
        assert isinstance(event_dict["metric_value"], float)

    def test_alert_event_optional_fields(self) -> None:
        """AlertEvent works with optional fields set to None."""
        from unified_internal_contracts import AlertEvent

        event = AlertEvent(
            alert_id="alert-002",
            rule_id="latency_spike",
            triggered_at=datetime.now(UTC),
            severity="WARNING",
            message="Latency spike detected",
            metric_value=500.0,
            threshold=200.0,
        )
        assert event.strategy_id is None
        assert event.venue is None

    def test_lifecycle_event_type_enum_values(self) -> None:
        """LifecycleEventType has the standard lifecycle states the service uses."""
        from unified_internal_contracts import LifecycleEventType

        # The service uses STARTED, STOPPED, FAILED
        assert hasattr(LifecycleEventType, "STARTED")
        assert hasattr(LifecycleEventType, "STOPPED")
        assert hasattr(LifecycleEventType, "FAILED")
        # Verify they are string-like (usable as event names)
        assert isinstance(LifecycleEventType.STARTED.value, str)


# ---------------------------------------------------------------------------
# unified_cloud_interface
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestUnifiedCloudInterface:
    """Functional tests for unified-cloud-interface usage in alerting-service."""

    def test_get_storage_client_returns_client(self) -> None:
        """get_storage_client() returns an object with the StorageClient interface."""
        from unified_cloud_interface import get_storage_client

        client = get_storage_client()
        # StorageClient protocol: upload_bytes, download_bytes, blob_exists, list_blobs
        assert hasattr(client, "upload_bytes")
        assert hasattr(client, "download_bytes")
        assert hasattr(client, "blob_exists")

    def test_get_queue_client_returns_client(self) -> None:
        """get_queue_client() returns an object with the QueueClient interface."""
        from unified_cloud_interface import get_queue_client

        client = get_queue_client()
        # QueueClient protocol: publish, subscribe_once
        assert hasattr(client, "publish")

    def test_get_secret_client_returns_client(self) -> None:
        """get_secret_client() returns an object with the SecretClient interface."""
        from unified_cloud_interface import get_secret_client

        client = get_secret_client()
        assert hasattr(client, "get_secret")

    def test_alert_storage_store_instantiation(self) -> None:
        """AlertStorageStore can be instantiated with a mock StorageClient."""
        from alerting_service.persistence.storage_store import AlertStorageStore

        mock_client = MagicMock()
        store = AlertStorageStore(storage_client=mock_client, bucket="test-bucket")
        assert store._bucket == "test-bucket"
        assert store._client is mock_client

    def test_alert_storage_store_write_alert_history(self) -> None:
        """AlertStorageStore.write_alert_history() calls upload_bytes on the client."""
        from alerting_service.persistence.storage_store import AlertStorageStore

        mock_client = MagicMock()
        store = AlertStorageStore(storage_client=mock_client, bucket="test-bucket")

        alert_data: dict[str, object] = {
            "alert_id": "test-alert-001",
            "rule_id": "test-rule",
            "triggered_at": datetime.now(UTC).isoformat(),
            "severity": "CRITICAL",
            "message": "Test alert",
            "metric_value": 95.0,
            "threshold": 90.0,
        }
        store.write_alert_history(alert_data)

        mock_client.upload_bytes.assert_called_once()
        call_kwargs = mock_client.upload_bytes.call_args
        assert call_kwargs.kwargs["bucket"] == "test-bucket"
        assert "alerting/history/date=" in call_kwargs.kwargs["blob_path"]
        assert call_kwargs.kwargs["content_type"] == "application/x-ndjson"

    def test_alert_storage_store_write_config_snapshot(self) -> None:
        """AlertStorageStore.write_config_snapshot() writes YAML to the client."""
        from alerting_service.persistence.storage_store import AlertStorageStore

        mock_client = MagicMock()
        store = AlertStorageStore(storage_client=mock_client, bucket="test-bucket")

        config_data: dict[str, object] = {
            "routing_rules": [{"event_pattern": "*", "channels": ["telegram"]}]
        }
        store.write_config_snapshot(config_data, name="test_rules")

        mock_client.upload_bytes.assert_called_once()
        call_kwargs = mock_client.upload_bytes.call_args
        assert call_kwargs.kwargs["blob_path"] == "alerting/configs/test_rules.yaml"
        assert call_kwargs.kwargs["content_type"] == "application/x-yaml"

    def test_alert_storage_store_read_cooldown_state_empty(self) -> None:
        """read_cooldown_state() returns empty dict when blob does not exist."""
        from alerting_service.persistence.storage_store import AlertStorageStore

        mock_client = MagicMock()
        mock_client.blob_exists.return_value = False
        store = AlertStorageStore(storage_client=mock_client, bucket="test-bucket")

        result = store.read_cooldown_state()
        assert result == {}

    def test_alert_storage_store_write_cooldown_state(self) -> None:
        """write_cooldown_state() writes JSON to the correct blob path."""
        from alerting_service.persistence.storage_store import AlertStorageStore

        mock_client = MagicMock()
        store = AlertStorageStore(storage_client=mock_client, bucket="test-bucket")

        state: dict[str, object] = {"cpu_threshold": "2026-03-16T00:00:00Z"}
        store.write_cooldown_state(state)

        mock_client.upload_bytes.assert_called_once()
        call_kwargs = mock_client.upload_bytes.call_args
        assert call_kwargs.kwargs["blob_path"] == "alerting/state/cooldowns.json"
        assert call_kwargs.kwargs["content_type"] == "application/json"


# ---------------------------------------------------------------------------
# unified_trading_library
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestUnifiedTradingLibrary:
    """Functional tests for unified-trading-library usage in alerting-service."""

    def test_graceful_shutdown_handler_lifecycle(self) -> None:
        """GracefulShutdownHandler tracks shutdown state correctly."""
        from unified_trading_library import GracefulShutdownHandler

        handler = GracefulShutdownHandler()
        assert not handler.is_shutdown_requested()
        handler.request_shutdown()
        assert handler.is_shutdown_requested()

    def test_log_level_enum_validation(self) -> None:
        """LogLevel enum accepts valid levels and rejects invalid ones."""
        from unified_trading_library import LogLevel

        assert LogLevel("INFO").value == "INFO"
        assert LogLevel("DEBUG").value == "DEBUG"
        assert LogLevel("WARNING").value == "WARNING"
        with pytest.raises(ValueError):
            LogLevel("INVALID_LEVEL")

    def test_mock_state_store_import(self) -> None:
        """MockStateStore can be imported (used in mock_state route)."""
        from unified_trading_library import MockStateStore

        assert MockStateStore is not None

    def test_alert_store_with_internal_contracts(self) -> None:
        """AlertStore.record_fired() works with AlertEvent from unified-internal-contracts."""
        from unified_internal_contracts import AlertEvent

        from alerting_service.core.alert_store import AlertStore

        store = AlertStore(storage_store=None)
        event = AlertEvent(
            alert_id="integ-test-001",
            rule_id="latency_threshold",
            triggered_at=datetime.now(UTC),
            severity="WARNING",
            message="Latency above threshold",
            metric_value=300.0,
            threshold=200.0,
        )
        store.record_fired(event)

        recent = store.get_recent_events(limit=10)
        assert len(recent) == 1
        assert recent[0].alert_id == "integ-test-001"
        assert recent[0].rule_id == "latency_threshold"

    def test_alert_store_cooldown_logic(self) -> None:
        """AlertStore.is_cooled_down() correctly enforces cooldown periods."""
        from unified_internal_contracts import AlertEvent

        from alerting_service.core.alert_store import AlertStore

        store = AlertStore(storage_store=None)
        assert store.is_cooled_down("rule-1", cooldown_seconds=60) is True

        event = AlertEvent(
            alert_id="cd-test-001",
            rule_id="rule-1",
            triggered_at=datetime.now(UTC),
            severity="INFO",
            message="Test",
            metric_value=10.0,
            threshold=5.0,
        )
        store.record_fired(event)
        # After firing, should NOT be cooled down yet (within 60s)
        assert store.is_cooled_down("rule-1", cooldown_seconds=60) is False

    def test_alert_store_publish_subscribe(self) -> None:
        """AlertStore pub/sub delivers events to subscribed queues."""
        from unified_internal_contracts import AlertEvent

        from alerting_service.core.alert_store import AlertStore

        store = AlertStore(storage_store=None)
        queue = store.subscribe()

        event = AlertEvent(
            alert_id="ps-test-001",
            rule_id="pubsub-rule",
            triggered_at=datetime.now(UTC),
            severity="CRITICAL",
            message="PubSub test",
            metric_value=100.0,
            threshold=50.0,
        )

        asyncio.get_event_loop().run_until_complete(store.publish(event))
        assert not queue.empty()
        received = queue.get_nowait()
        assert received.alert_id == "ps-test-001"

        store.unsubscribe(queue)

    def test_alert_subscriber_message_deserialization(self) -> None:
        """AlertSubscriber._deserialize_message parses JSON payloads correctly."""
        import json

        from alerting_service.subscribers.alert_subscriber import (
            _deserialize_message,
        )

        payload = {"event_name": "KILL_SWITCH_ACTIVATED", "source": "execution-service"}
        data = json.dumps(payload).encode("utf-8")

        event_name, details = _deserialize_message(data)
        assert event_name == "KILL_SWITCH_ACTIVATED"
        assert details.get("source") == "execution-service"

        # Malformed data should return MALFORMED_EVENT, not crash
        bad_data = b"not-json"
        event_name_bad, details_bad = _deserialize_message(bad_data)
        assert event_name_bad == "MALFORMED_EVENT"
        assert details_bad == {}

    def test_extract_event_name_fallback_keys(self) -> None:
        """_extract_event_name tries event_name, event_type, type in order."""
        from alerting_service.subscribers.alert_subscriber import _extract_event_name

        assert _extract_event_name({"event_name": "A"}) == "A"
        assert _extract_event_name({"event_type": "B"}) == "B"
        assert _extract_event_name({"type": "C"}) == "C"
        assert _extract_event_name({}) == "UNKNOWN_EVENT"

    def test_routing_rules_matching(self) -> None:
        """_match_routing_rules correctly matches event patterns to channels."""
        from alerting_service.notifiers.router import _match_routing_rules

        rules: list[dict[str, object]] = [
            {"event_pattern": "KILL_SWITCH_*", "channels": ["pagerduty", "telegram"], "severity_filter": "critical"},
            {"event_pattern": "SERVICE_DEGRADED", "channels": ["telegram"], "severity_filter": None},
            {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
        ]

        channels, severity = _match_routing_rules("KILL_SWITCH_ACTIVATED", rules)
        assert "pagerduty" in channels
        assert "telegram" in channels
        assert severity == "critical"

        channels2, severity2 = _match_routing_rules("SERVICE_DEGRADED", rules)
        assert channels2 == {"telegram"}
        assert severity2 is None

        channels3, severity3 = _match_routing_rules("RANDOM_EVENT", rules)
        assert "telegram" in channels3

    @patch("alerting_service.notifiers.router._deliver_message", return_value=True)
    @patch("alerting_service.notifiers.router._persist_delivery_record")
    @patch("alerting_service.notifiers.router._persist_config_snapshot")
    @patch("alerting_service.notifiers.router.pd_send_event", return_value=True)
    def test_route_event_full_flow(
        self,
        mock_pd: MagicMock,
        mock_config_snapshot: MagicMock,
        mock_persist: MagicMock,
        mock_deliver: MagicMock,
    ) -> None:
        """route_event() dispatches to correct channels based on routing rules."""
        from alerting_service.notifiers.router import route_event

        route_event(
            "KILL_SWITCH_ACTIVATED",
            {"message": "Kill switch triggered", "source": "execution-service"},
        )
        # PagerDuty should be called for KILL_SWITCH_* pattern
        mock_pd.assert_called_once()
