"""Unit tests for AlertSubscriber."""

import json
from unittest.mock import MagicMock, patch

import pytest

from alerting_service.subscribers.alert_subscriber import (
    _ALERT_SUBSCRIPTIONS,
    AlertSubscriber,
    _deserialize_message,
    _extract_event_name,
    _page_own_dispatch_failure,
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

    def test_utl_envelope_flattened_to_top_level(self) -> None:
        """REGRESSION (data_completion_to_100_all_ag_2026_06_21 — generic-alert bug):
        the UTL ``log_event`` PubSub envelope nests the emitter's real payload at
        ``metadata.details`` and severity at ``metadata.severity``. The subscriber
        MUST flatten it so the router + data_pipeline_slack formatter read
        vm_name / exit_code / error_message / severity / umbrella at the TOP level
        — otherwise every DP_* alert renders generic (event name only).
        """
        data = json.dumps(
            {
                "event": "DP_VM_EXIT_NONZERO",
                "service": "deployment-service",
                "metadata": {
                    "timestamp": "2026-06-23T16:48:00+00:00",
                    "service_name": "exit-code-fleet-monitor",
                    "severity": "CRITICAL",
                    "correlation_id": "abc-123",
                    "details": {
                        "vm_name": "instr-backfill-sports-001",
                        "exit_code": 137,
                        "umbrella": "BATCH",
                        "asset_group": "sports",
                        "oom": True,
                        "run_log_tail": "OOMKilled\nrc=137",
                        "message": "VM instr-backfill-sports-001 terminated with exit_code=137 (OOM)",
                    },
                },
            }
        ).encode()
        event_name, details = _deserialize_message(data)
        assert event_name == "DP_VM_EXIT_NONZERO"
        # The emitter's real payload is now TOP-LEVEL (what the formatter reads).
        assert details["vm_name"] == "instr-backfill-sports-001"
        assert details["exit_code"] == 137
        assert details["umbrella"] == "BATCH"
        assert details["asset_group"] == "sports"
        assert details["run_log_tail"] == "OOMKilled\nrc=137"
        assert details["message"].startswith("VM instr-backfill-sports-001 terminated")
        # Envelope-level severity + correlation_id promoted to top level.
        assert details["severity"] == "CRITICAL"
        assert details["correlation_id"] == "abc-123"
        # The nested envelope keys are NOT left at the top level.
        assert "metadata" not in details

    def test_flat_legacy_payload_passthrough_unchanged(self) -> None:
        """A flat legacy payload (kill-switch / margin emitters, no ``metadata``
        envelope) is returned UNCHANGED — the unwrap only fires on the UTL shape."""
        data = json.dumps({"event_name": "KILL_SWITCH_ACTIVATED", "venue": "binance", "scope": "FIRM"}).encode()
        event_name, details = _deserialize_message(data)
        assert event_name == "KILL_SWITCH_ACTIVATED"
        assert details["venue"] == "binance"
        assert details["scope"] == "FIRM"


class TestAlertSubscriber:
    def _make_subscriber(self, project_id: str = "test-project") -> AlertSubscriber:
        with patch("alerting_service.subscribers.alert_subscriber.get_queue_client") as mock_factory:
            mock_factory.return_value = MagicMock()
            subscriber = AlertSubscriber(project_id=project_id)
        return subscriber

    def test_default_subscriptions(self) -> None:
        subscriber = self._make_subscriber()
        assert subscriber._subscriptions == _ALERT_SUBSCRIPTIONS

    def test_default_subscriptions_include_service_lifecycle_events(self) -> None:
        # Operator ruling 2026-07-29: subscribe alerting-service to the
        # UAC-canonical service-lifecycle-events topic too, alongside the
        # legacy lifecycle-events-sub — see
        # plans/active/issues/live_mode_event_sink_topic_missing_2026_06_21.md.
        subscriber = self._make_subscriber()
        assert "service-lifecycle-events-sub" in subscriber._subscriptions
        assert "lifecycle-events-sub" in subscriber._subscriptions

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

    def test_dispatch_failure_is_paged_not_just_logged(self) -> None:
        """REGRESSION (defi_consolidator_cron_left_paused_2026_07_15): a
        ``dispatch_event`` failure inside ``_route_one`` must page via
        ``_page_own_dispatch_failure`` in addition to the existing
        ``logger.warning`` — a broken alert route must be loud about its OWN
        failure ("a dead digest must not be silent"), not silently skip.

        Calls ``_route_one`` directly (it is synchronous) rather than driving
        the full ``stream()`` async generator: a failed message is never
        yielded, so an ``async for`` loop that only calls ``stop()`` in its
        body would never terminate.
        """
        payload = json.dumps({"event_name": "PREFLIGHT_FAILED", "session": "2026-01-01"}).encode()
        subscriber = self._make_subscriber()

        with (
            patch(
                "alerting_service.subscribers.alert_subscriber.route_event",
                side_effect=RuntimeError("PagerDuty misconfigured"),
            ),
            patch("alerting_service.subscribers.alert_subscriber._page_own_dispatch_failure") as mock_page,
        ):
            result = subscriber._route_one(payload, {"source": "test"}, "risk_alerts_circuit_breaker_triggers")

        # The failed message is skipped (not routed) — same as before this fix.
        assert result is None
        mock_page.assert_called_once()
        call_args = mock_page.call_args.args
        assert call_args[0] == "risk_alerts_circuit_breaker_triggers"
        assert call_args[1] == "PREFLIGHT_FAILED"
        assert isinstance(call_args[2], RuntimeError)


class TestPageOwnDispatchFailure:
    def test_pages_via_uts_live_alerts_webhook_when_configured(self) -> None:
        """When a webhook is configured, the failure is posted directly to
        #uts-live-alerts — bypassing dispatch_event/route_event, the path that
        just failed."""
        mock_config = MagicMock()
        mock_config.uts_live_alerts_slack_webhook = "https://hooks.example/webhook"

        with (
            patch(
                "alerting_service.subscribers.alert_subscriber._get_subscriber_alerting_config",
                return_value=mock_config,
            ),
            patch(
                "alerting_service.subscribers.alert_subscriber.get_paging_credentials",
                return_value={},
            ),
            patch("alerting_service.subscribers.alert_subscriber.send_uts_live_alert") as mock_send,
        ):
            _page_own_dispatch_failure("lifecycle-events-sub", "CONSOLIDATOR_DOWN", RuntimeError("boom"))

        mock_send.assert_called_once()
        args = mock_send.call_args.args
        assert args[0] == "https://hooks.example/webhook"
        assert args[1] == "ALERT_DISPATCH_FAILED"
        assert "lifecycle-events-sub" in args[2]
        assert "CONSOLIDATOR_DOWN" in args[2]

    def test_never_raises_when_no_webhook_configured(self) -> None:
        """No webhook configured → loud logger.error, never a raise (mock/CI safe)."""
        mock_config = MagicMock()
        mock_config.uts_live_alerts_slack_webhook = ""

        with (
            patch(
                "alerting_service.subscribers.alert_subscriber._get_subscriber_alerting_config",
                return_value=mock_config,
            ),
            patch(
                "alerting_service.subscribers.alert_subscriber.get_paging_credentials",
                return_value={},
            ),
            patch("alerting_service.subscribers.alert_subscriber.send_uts_live_alert") as mock_send,
        ):
            _page_own_dispatch_failure("some_sub", None, RuntimeError("boom"))

        mock_send.assert_not_called()

    def test_never_raises_when_paging_itself_fails(self) -> None:
        """Defence-in-depth: a broken paging path must not crash the caller
        (the subscriber loop that is already recovering from the original
        dispatch error)."""
        with patch(
            "alerting_service.subscribers.alert_subscriber._get_subscriber_alerting_config",
            side_effect=RuntimeError("config store unreachable"),
        ):
            _page_own_dispatch_failure("some_sub", "SOME_EVENT", RuntimeError("boom"))  # must not raise
