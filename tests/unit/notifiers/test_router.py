"""Unit tests for the event router."""

from unittest.mock import MagicMock, patch

import pytest

from alerting_service.notifiers.router import (
    _match_routing_rules,
    route_event,
)


@pytest.fixture
def mock_pd_send_event():
    """Patch PagerDuty send_event; returns True by default."""
    with patch("alerting_service.notifiers.router.pd_send_event", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_slack_send_message():
    """Patch Slack send_message; returns True by default."""
    with patch("alerting_service.notifiers.router.slack_send_message", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_send_telegram():
    """Patch Telegram send_telegram; returns True by default."""
    with patch("alerting_service.notifiers.router.send_telegram", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_log_event():
    """Suppress log_event calls."""
    with patch("alerting_service.notifiers.router.log_event") as mock:
        yield mock


@pytest.fixture
def mock_persist_delivery():
    """Suppress delivery record persistence."""
    with patch("alerting_service.notifiers.router._persist_delivery_record") as mock:
        yield mock


@pytest.fixture
def mock_persist_config():
    """Suppress config snapshot persistence."""
    with patch("alerting_service.notifiers.router._persist_config_snapshot") as mock:
        yield mock


@pytest.fixture
def mock_config_with_telegram():
    """Patch AlertingSystemConfig to provide Telegram credentials + default rules."""
    mock_cfg = MagicMock()
    mock_cfg.telegram_bot_token = "bot-token-123"
    mock_cfg.telegram_chat_id = "chat-456"
    mock_cfg.telegram_chat_id_ops = ""
    mock_cfg.gcp_project_id = "test-project"
    mock_cfg.pagerduty_disabled = False
    mock_cfg.quietness_baseline_mode = False
    # Default routing rules matching legacy behavior
    mock_cfg.routing_rules = [
        {
            "event_pattern": "KILL_SWITCH_*",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        {
            "event_pattern": "CIRCUIT_BREAKER_OPEN",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        {"event_pattern": "PREFLIGHT_FAILED", "channels": ["telegram"], "severity_filter": None},
        {"event_pattern": "SERVICE_DEGRADED", "channels": ["telegram"], "severity_filter": None},
        {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
    ]
    with patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=mock_cfg):
        yield mock_cfg


@pytest.fixture
def mock_config_without_telegram():
    """Patch AlertingSystemConfig with no Telegram credentials."""
    mock_cfg = MagicMock()
    mock_cfg.telegram_bot_token = ""
    mock_cfg.telegram_chat_id = ""
    mock_cfg.telegram_chat_id_ops = ""
    mock_cfg.gcp_project_id = "test-project"
    mock_cfg.pagerduty_disabled = False
    mock_cfg.quietness_baseline_mode = False
    mock_cfg.routing_rules = [
        {
            "event_pattern": "KILL_SWITCH_*",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
    ]
    with patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=mock_cfg):
        yield mock_cfg


class TestMatchRoutingRules:
    """Tests for the _match_routing_rules function."""

    def test_exact_match(self) -> None:
        rules: list[dict[str, object]] = [
            {
                "event_pattern": "CIRCUIT_BREAKER_OPEN",
                "channels": ["pagerduty", "telegram"],
                "severity_filter": "critical",
            },
        ]
        channels, severity = _match_routing_rules("CIRCUIT_BREAKER_OPEN", rules)
        assert channels == {"pagerduty", "telegram"}
        assert severity == "critical"

    def test_glob_match(self) -> None:
        rules: list[dict[str, object]] = [
            {
                "event_pattern": "KILL_SWITCH_*",
                "channels": ["pagerduty", "telegram"],
                "severity_filter": "critical",
            },
        ]
        channels, severity = _match_routing_rules("KILL_SWITCH_ACTIVATED", rules)
        assert channels == {"pagerduty", "telegram"}
        assert severity == "critical"

    def test_wildcard_catch_all(self) -> None:
        rules: list[dict[str, object]] = [
            {
                "event_pattern": "KILL_SWITCH_*",
                "channels": ["pagerduty", "telegram"],
                "severity_filter": "critical",
            },
            {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
        ]
        channels, severity = _match_routing_rules("SOME_OTHER_EVENT", rules)
        assert channels == {"telegram"}
        assert severity is None

    def test_first_match_wins(self) -> None:
        rules: list[dict[str, object]] = [
            {
                "event_pattern": "KILL_SWITCH_*",
                "channels": ["pagerduty"],
                "severity_filter": "critical",
            },
            {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
        ]
        # KILL_SWITCH_ACTIVATED matches the first rule, not the catch-all
        channels, severity = _match_routing_rules("KILL_SWITCH_ACTIVATED", rules)
        assert channels == {"pagerduty"}
        assert severity == "critical"

    def test_no_rules_falls_back_to_telegram(self) -> None:
        channels, severity = _match_routing_rules("ANYTHING", [])
        assert channels == {"telegram"}
        assert severity is None

    def test_no_severity_filter(self) -> None:
        rules: list[dict[str, object]] = [
            {"event_pattern": "INFO_*", "channels": ["telegram"], "severity_filter": None},
        ]
        channels, severity = _match_routing_rules("INFO_UPDATE", rules)
        assert channels == {"telegram"}
        assert severity is None

    def test_invalid_severity_defaults_to_warning(self) -> None:
        rules: list[dict[str, object]] = [
            {
                "event_pattern": "BAD_SEVERITY_*",
                "channels": ["pagerduty"],
                "severity_filter": "catastrophic",
            },
        ]
        channels, severity = _match_routing_rules("BAD_SEVERITY_EVENT", rules)
        assert channels == {"pagerduty"}
        assert severity == "warning"

    def test_valid_severity_preserved(self) -> None:
        rules: list[dict[str, object]] = [
            {
                "event_pattern": "TEST_*",
                "channels": ["pagerduty"],
                "severity_filter": "error",
            },
        ]
        channels, severity = _match_routing_rules("TEST_EVENT", rules)
        assert channels == {"pagerduty"}
        assert severity == "error"

    def test_severity_case_insensitive(self) -> None:
        rules: list[dict[str, object]] = [
            {
                "event_pattern": "UPPER_*",
                "channels": ["pagerduty"],
                "severity_filter": "CRITICAL",
            },
        ]
        channels, severity = _match_routing_rules("UPPER_EVENT", rules)
        assert channels == {"pagerduty"}
        assert severity == "critical"


class TestRouteEventWithTelegram:
    """Tests when Telegram is configured (primary path)."""

    def test_kill_switch_sends_to_pagerduty_and_telegram(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("KILL_SWITCH_ACTIVATED", {"strategy": "s1"})

        mock_pd_send_event.assert_called_once()
        pd_kwargs = mock_pd_send_event.call_args.kwargs
        assert pd_kwargs["severity"] == "critical"
        assert "KILL_SWITCH_ACTIVATED" in pd_kwargs["summary"]

        mock_send_telegram.assert_called_once()
        mock_slack_send_message.assert_not_called()

    def test_circuit_breaker_sends_to_pagerduty_and_telegram(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("CIRCUIT_BREAKER_OPEN", {"venue": "binance"})

        mock_pd_send_event.assert_called_once()
        mock_send_telegram.assert_called_once()
        mock_slack_send_message.assert_not_called()

    def test_preflight_failed_sends_to_telegram_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"session": "2026-01-01"})

        mock_pd_send_event.assert_not_called()
        mock_send_telegram.assert_called_once()
        mock_slack_send_message.assert_not_called()

    def test_service_degraded_sends_to_telegram_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("SERVICE_DEGRADED", {"service": "market-data"})

        mock_pd_send_event.assert_not_called()
        mock_send_telegram.assert_called_once()
        mock_slack_send_message.assert_not_called()

    def test_unknown_event_goes_to_telegram(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("SOME_OTHER_EVENT", {})

        mock_pd_send_event.assert_not_called()
        mock_send_telegram.assert_called_once()
        mock_slack_send_message.assert_not_called()

    def test_telegram_receives_bot_token_and_chat_id(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"msg": "test"})

        tg_kwargs = mock_send_telegram.call_args.kwargs
        assert tg_kwargs["bot_token"] == "bot-token-123"
        assert tg_kwargs["chat_id"] == "chat-456"

    def test_details_forwarded_to_pagerduty(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        details: dict[str, object] = {"venue": "binance", "order_id": "ord-42"}
        route_event("CIRCUIT_BREAKER_OPEN", details)

        pd_kwargs = mock_pd_send_event.call_args.kwargs
        assert pd_kwargs["details"] == details


class TestRouteEventSlackFallback:
    """Tests when Telegram is NOT configured (falls back to Slack)."""

    def test_falls_back_to_slack_when_telegram_not_configured(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_without_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"session": "2026-01-01"})

        mock_send_telegram.assert_not_called()
        mock_slack_send_message.assert_called_once()

    def test_pagerduty_still_fires_without_telegram(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_without_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("KILL_SWITCH_ACTIVATED", {"strategy": "s1"})

        mock_pd_send_event.assert_called_once()
        mock_slack_send_message.assert_called_once()


class TestRouteEventDedup:
    """Tests for deduplication integration."""

    def test_duplicate_event_is_suppressed(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"session": "s1"})
        route_event("PREFLIGHT_FAILED", {"session": "s1"})

        # Only one Telegram call despite two route_event calls
        assert mock_send_telegram.call_count == 1

    def test_different_details_not_suppressed(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"session": "s1"})
        route_event("PREFLIGHT_FAILED", {"session": "s2"})

        assert mock_send_telegram.call_count == 2


class TestRouteEventFailureHandling:
    def test_pagerduty_failure_is_logged_not_raised(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        mock_pd_send_event.return_value = False
        # Must not raise even when PagerDuty fails
        route_event("CIRCUIT_BREAKER_OPEN", {})

    def test_telegram_failure_is_logged_not_raised(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        mock_send_telegram.return_value = False
        # Must not raise even when Telegram fails
        route_event("PREFLIGHT_FAILED", {})

    def test_slack_failure_is_logged_not_raised(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_without_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        mock_slack_send_message.return_value = False
        # Must not raise even when Slack fails
        route_event("PREFLIGHT_FAILED", {})


class TestDeliveryRecordPersistence:
    """Tests that delivery records are persisted via _persist_delivery_record."""

    def test_delivery_record_persisted_on_telegram_success(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"session": "s1"})

        # One delivery record for telegram
        assert mock_persist_delivery.call_count == 1
        record = mock_persist_delivery.call_args[0][0]
        assert record["channel"] == "telegram"
        assert record["status"] == "sent"
        assert "alert_id" in record
        assert record["event_name"] == "PREFLIGHT_FAILED"

    def test_delivery_records_persisted_for_pagerduty_and_telegram(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("KILL_SWITCH_ACTIVATED", {"strategy": "s1"})

        # Two delivery records: pagerduty + telegram
        assert mock_persist_delivery.call_count == 2
        channels = {call[0][0]["channel"] for call in mock_persist_delivery.call_args_list}
        assert channels == {"pagerduty", "telegram"}

    def test_failed_delivery_records_have_failed_status(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        mock_send_telegram.return_value = False
        route_event("PREFLIGHT_FAILED", {})

        record = mock_persist_delivery.call_args[0][0]
        assert record["status"] == "failed"


class TestConfigSnapshotPersistence:
    """Tests that config snapshots are persisted."""

    def test_config_snapshot_persisted_on_route(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_config_with_telegram: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"session": "s1"})

        mock_persist_config.assert_called_once()


class TestDefiRoutingRules:
    """Tests for DeFi event routing through _match_routing_rules."""

    @staticmethod
    def _defi_rules() -> list[dict[str, object]]:
        """Return routing rules including DeFi entries (matches config.py defaults)."""
        return [
            {
                "event_pattern": "KILL_SWITCH_*",
                "channels": ["pagerduty", "telegram"],
                "severity_filter": "critical",
            },
            {
                "event_pattern": "DEFI_HEALTH_FACTOR_CRITICAL",
                "channels": ["pagerduty", "telegram"],
                "severity_filter": "critical",
            },
            {
                "event_pattern": "DEFI_WEETH_DEPEG",
                "channels": ["pagerduty", "telegram"],
                "severity_filter": "critical",
            },
            {
                "event_pattern": "DEFI_AAVE_UTILIZATION_SPIKE",
                "channels": ["telegram"],
                "severity_filter": None,
            },
            {
                "event_pattern": "DEFI_FUNDING_RATE_FLIP",
                "channels": ["telegram"],
                "severity_filter": None,
            },
            {
                "event_pattern": "DEFI_FEATURE_STALE",
                "channels": ["telegram"],
                "severity_filter": None,
            },
            {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
        ]

    def test_health_factor_critical_routes_to_pagerduty_and_telegram(self) -> None:
        channels, severity = _match_routing_rules("DEFI_HEALTH_FACTOR_CRITICAL", self._defi_rules())
        assert channels == {"pagerduty", "telegram"}
        assert severity == "critical"

    def test_weeth_depeg_routes_to_pagerduty_and_telegram(self) -> None:
        channels, severity = _match_routing_rules("DEFI_WEETH_DEPEG", self._defi_rules())
        assert channels == {"pagerduty", "telegram"}
        assert severity == "critical"

    def test_aave_utilization_routes_to_telegram_only(self) -> None:
        channels, severity = _match_routing_rules("DEFI_AAVE_UTILIZATION_SPIKE", self._defi_rules())
        assert channels == {"telegram"}
        assert severity is None

    def test_funding_rate_flip_routes_to_telegram_only(self) -> None:
        channels, severity = _match_routing_rules("DEFI_FUNDING_RATE_FLIP", self._defi_rules())
        assert channels == {"telegram"}
        assert severity is None

    def test_feature_stale_routes_to_telegram_only(self) -> None:
        channels, severity = _match_routing_rules("DEFI_FEATURE_STALE", self._defi_rules())
        assert channels == {"telegram"}
        assert severity is None


class TestCustomRoutingRules:
    """Tests with custom routing rules."""

    def test_custom_rule_routes_to_pagerduty_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
    ) -> None:
        mock_cfg = MagicMock()
        mock_cfg.telegram_bot_token = "bot-token-123"
        mock_cfg.telegram_chat_id = "chat-456"
        mock_cfg.gcp_project_id = "test-project"
        mock_cfg.pagerduty_disabled = False
        mock_cfg.quietness_baseline_mode = False
        mock_cfg.routing_rules = [
            {"event_pattern": "CUSTOM_*", "channels": ["pagerduty"], "severity_filter": "warning"},
        ]
        with patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=mock_cfg):
            route_event("CUSTOM_EVENT", {"msg": "test"})

        mock_pd_send_event.assert_called_once()
        pd_kwargs = mock_pd_send_event.call_args.kwargs
        assert pd_kwargs["severity"] == "warning"
        # telegram not in channels, so send_telegram not called
        mock_send_telegram.assert_not_called()


class TestTelegramOpsChannelRouting:
    """Tests for dual-channel Telegram routing (ops vs standard channel)."""

    @pytest.fixture
    def mock_config_with_ops_channel(self):
        """AlertingSystemConfig with both standard and ops Telegram channels."""
        from alerting_service.notifiers.router import _get_cloud_config

        _get_cloud_config.cache_clear()
        mock_cfg = MagicMock()
        mock_cfg.telegram_bot_token = "bot-token-123"
        mock_cfg.telegram_chat_id = "chat-456"
        mock_cfg.telegram_chat_id_ops = "ops-chat-789"
        mock_cfg.gcp_project_id = "test-project"
        mock_cfg.pagerduty_disabled = False
        mock_cfg.quietness_baseline_mode = False
        mock_cfg.routing_rules = [
            {
                "event_pattern": "KILL_SWITCH_*",
                "channels": ["pagerduty", "telegram"],
                "severity_filter": "critical",
            },
            {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
        ]
        with patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=mock_cfg):
            yield mock_cfg
        _get_cloud_config.cache_clear()

    def test_ops_channel_used_for_runtime_alert_when_configured(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        mock_config_with_ops_channel: MagicMock,
    ) -> None:
        # Use a real AlertCode that is explicitly in LIVE_ALERT_RULES (not just the catch-all *).
        # KILL_SWITCH_ACTIVATED is not a UAC AlertCode — use KILL_SWITCH_VENUE_DISCONNECT instead.
        route_event("KILL_SWITCH_VENUE_DISCONNECT", {"strategy": "s1"})

        mock_send_telegram.assert_called_once()
        tg_kwargs = mock_send_telegram.call_args.kwargs
        assert tg_kwargs["chat_id"] == "ops-chat-789"
        assert tg_kwargs["bot_token"] == "bot-token-123"

    def test_standard_channel_used_when_ops_not_configured(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        mock_config_with_telegram: MagicMock,
    ) -> None:
        route_event("KILL_SWITCH_VENUE_DISCONNECT", {"strategy": "s1"})

        mock_send_telegram.assert_called_once()
        tg_kwargs = mock_send_telegram.call_args.kwargs
        assert tg_kwargs["chat_id"] == "chat-456"

    def test_standard_channel_used_for_non_live_alert(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_telegram: MagicMock,
        mock_slack_send_message: MagicMock,
        mock_log_event: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        mock_config_with_ops_channel: MagicMock,
    ) -> None:
        # "INTERNAL_QG_HEALTH" is NOT in LIVE_ALERT_RULES — standard channel applies
        route_event("INTERNAL_QG_HEALTH", {"check": "ping"})

        mock_send_telegram.assert_called_once()
        tg_kwargs = mock_send_telegram.call_args.kwargs
        assert tg_kwargs["chat_id"] == "chat-456"
