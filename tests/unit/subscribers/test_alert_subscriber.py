"""Unit tests for AlertSubscriber."""

import json
from unittest.mock import MagicMock, patch

import pytest

from alerting_service.subscribers.alert_subscriber import (
    _ALERT_SUBSCRIPTIONS,
    AlertSubscriber,
    _deserialize_message,
    _extract_event_name,
)


class TestExtractEventName:
    def test_event_name_key_takes_priority(self) -> None:
        payload: dict[str, object] = {
            "event_name": "KILL_SWITCH_ACTIVATED",
            "event_type": "OTHER",
            "type": "ANOTHER",
        }
        assert _extract_event_name(payload) == "KILL_SWITCH_ACTIVATED"

    def test_event_type_key_used_when_no_event_name(self) -> None:
        payload: dict[str, object] = {"event_type": "CIRCUIT_BREAKER_OPEN"}
        assert _extract_event_name(payload) == "CIRCUIT_BREAKER_OPEN"

    def test_type_key_used_as_fallback(self) -> None:
        payload: dict[str, object] = {"type": "PREFLIGHT_FAILED"}
        assert _extract_event_name(payload) == "PREFLIGHT_FAILED"

    def test_unknown_event_when_no_key_present(self) -> None:
        assert _extract_event_name({}) == "UNKNOWN_EVENT"

    def test_empty_string_event_name_skipped(self) -> None:
        payload: dict[str, object] = {"event_name": "", "event_type": "REAL_EVENT"}
        assert _extract_event_name(payload) == "REAL_EVENT"

    def test_utl_event_key_extracted(self) -> None:
        """REGRESSION (dp_event_pubsub_delivery_gap_2026_06_22): UTL ``log_event`` /
        ``PubSubEventSink.write_event`` publishes the name under ``event`` —
        ``{"event": name, "service": ..., "metadata": {...}}``. The extractor MUST
        read ``event`` or every DP_* / CONSOLIDATOR_DOWN on lifecycle-events
        mis-extracts to UNKNOWN_EVENT and is silently dropped before Slack.
        """
        payload: dict[str, object] = {
            "event": "DP_DIVERGENT_EMPTY",
            "service": "dp_audit",
            "metadata": {"severity": "WARN", "details": {"asset_group": "defi"}},
        }
        assert _extract_event_name(payload) == "DP_DIVERGENT_EMPTY"

    def test_event_key_priority_over_legacy_keys(self) -> None:
        """``event`` (the UTL canonical key) wins over the legacy fallbacks."""
        payload: dict[str, object] = {"event": "DP_EMPTY_REPROBE_DISAGREEMENT", "type": "OTHER"}
        assert _extract_event_name(payload) == "DP_EMPTY_REPROBE_DISAGREEMENT"


class TestDeserializeMessage:
    def test_valid_payload(self) -> None:
        data = json.dumps({"event_name": "KILL_SWITCH_ACTIVATED", "venue": "binance"}).encode()
        event_name, details = _deserialize_message(data)
        assert event_name == "KILL_SWITCH_ACTIVATED"
        assert details["venue"] == "binance"

    def test_malformed_json_returns_malformed_event(self) -> None:
        event_name, details = _deserialize_message(b"not-json{{{")
        assert event_name == "MALFORMED_EVENT"
        assert details == {}

    def test_invalid_utf8_returns_malformed_event(self) -> None:
        event_name, details = _deserialize_message(b"\xff\xfe")
        assert event_name == "MALFORMED_EVENT"
        assert details == {}

    def test_missing_event_key_returns_unknown(self) -> None:
        data = json.dumps({"message": "hello"}).encode()
        event_name, _ = _deserialize_message(data)
        assert event_name == "UNKNOWN_EVENT"


class TestAlertSubscriber:
    def _make_subscriber(self, project_id: str = "test-project") -> AlertSubscriber:
        with patch("alerting_service.subscribers.alert_subscriber.get_queue_client") as mock_factory:
            mock_factory.return_value = MagicMock()
            subscriber = AlertSubscriber(project_id=project_id)
        return subscriber

    def test_default_subscriptions(self) -> None:
        subscriber = self._make_subscriber()
        assert subscriber._subscriptions == _ALERT_SUBSCRIPTIONS

    def test_custom_subscriptions(self) -> None:
        with patch("alerting_service.subscribers.alert_subscriber.get_queue_client"):
            subscriber = AlertSubscriber(
                project_id="p",
                subscriptions=("topic_a", "topic_b"),
            )
        assert subscriber._subscriptions == ("topic_a", "topic_b")

    def test_stop_sets_running_false(self) -> None:
        subscriber = self._make_subscriber()
        subscriber._running = True
        subscriber.stop()
        assert subscriber._running is False

    @pytest.mark.asyncio
    async def test_stream_yields_events_and_calls_route_event(self) -> None:
        """Single message round-trip: deserialize → route_event → yield."""
        payload = json.dumps({"event_name": "PREFLIGHT_FAILED", "session": "2026-01-01"})
        message_bytes = payload.encode()

        mock_client = MagicMock()
        # First call returns one message; second call returns empty to allow stop.
        mock_client.subscribe_once.side_effect = [
            [(message_bytes, {"source": "test"})],
            [],
        ]

        with (
            patch(
                "alerting_service.subscribers.alert_subscriber.get_queue_client",
                return_value=mock_client,
            ),
            patch("alerting_service.subscribers.alert_subscriber.route_event") as mock_route,
            patch("alerting_service.subscribers.alert_subscriber.log_event"),
        ):
            subscriber = AlertSubscriber(
                project_id="test-project",
                subscriptions=("risk_alerts_circuit_breaker_triggers",),
                poll_interval_seconds=0.0,
            )

            results: list[tuple[str, dict[str, object]]] = []
            async for event_name, details in subscriber.stream():
                results.append((event_name, details))
                subscriber.stop()  # stop after first event

        assert len(results) == 1
        assert results[0][0] == "PREFLIGHT_FAILED"
        mock_route.assert_called_once()
        call_args = mock_route.call_args
        assert call_args.args[0] == "PREFLIGHT_FAILED"

    @pytest.mark.asyncio
    async def test_malformed_message_skipped_without_crash(self) -> None:
        """Malformed messages are logged and skipped; loop continues."""
        mock_client = MagicMock()
        mock_client.subscribe_once.side_effect = [
            [(b"bad json{{{", {})],
            [],
        ]

        with (
            patch(
                "alerting_service.subscribers.alert_subscriber.get_queue_client",
                return_value=mock_client,
            ),
            patch("alerting_service.subscribers.alert_subscriber.route_event") as mock_route,
            patch("alerting_service.subscribers.alert_subscriber.log_event"),
        ):
            subscriber = AlertSubscriber(
                project_id="test-project",
                subscriptions=("risk_alerts_circuit_breaker_triggers",),
                poll_interval_seconds=0.0,
            )

            results: list[tuple[str, dict[str, object]]] = []
            async for event_name, details in subscriber.stream():
                results.append((event_name, details))
                subscriber.stop()

        # MALFORMED_EVENT is still routed (route_event decides what to do with it)
        assert len(results) == 1
        assert results[0][0] == "MALFORMED_EVENT"
        mock_route.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_until_stopped_completes_without_error(self) -> None:
        """run_until_stopped() returns after stop() is called."""
        mock_client = MagicMock()
        mock_client.subscribe_once.return_value = []

        with (
            patch(
                "alerting_service.subscribers.alert_subscriber.get_queue_client",
                return_value=mock_client,
            ),
            patch("alerting_service.subscribers.alert_subscriber.route_event"),
            patch("alerting_service.subscribers.alert_subscriber.log_event"),
        ):
            import asyncio

            subscriber = AlertSubscriber(
                project_id="test-project",
                subscriptions=("risk_alerts_circuit_breaker_triggers",),
                poll_interval_seconds=0.0,
            )

            async def _stop_after_one_tick() -> None:
                await asyncio.sleep(0)
                subscriber.stop()

            await asyncio.gather(
                subscriber.run_until_stopped(),
                _stop_after_one_tick(),
            )
