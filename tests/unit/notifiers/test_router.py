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
def mock_send_uts_live_alert():
    """Patch the primary Slack notifier; returns None by default (no exception = success)."""
    with patch("alerting_service.notifiers.router.send_uts_live_alert") as mock:
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
def empty_paging_creds():
    """Return empty SM creds so config-level webhook is authoritative."""
    with patch(
        "alerting_service.notifiers.router.get_paging_credentials",
        return_value={"uts_live_alerts_slack_webhook": ""},
    ) as mock:
        yield mock


@pytest.fixture
def mock_config_slack_only():
    """AlertingSystemConfig with Slack webhook + standard routing rules (Telegram retired)."""
    mock_cfg = MagicMock()
    mock_cfg.uts_live_alerts_slack_webhook = "https://hooks.slack.com/services/T/B/test"
    mock_cfg.data_pipeline_slack_webhook = ""
    mock_cfg.gcp_project_id = "test-project"
    mock_cfg.pagerduty_disabled = False
    mock_cfg.quietness_baseline_mode = False
    mock_cfg.routing_rules = [
        {
            "event_pattern": "KILL_SWITCH_*",
            "channels": ["pagerduty", "slack"],
            "severity_filter": "critical",
        },
        {
            "event_pattern": "CIRCUIT_BREAKER_OPEN",
            "channels": ["pagerduty", "slack"],
            "severity_filter": "critical",
        },
        {"event_pattern": "PREFLIGHT_FAILED", "channels": ["slack"], "severity_filter": None},
        {"event_pattern": "SERVICE_DEGRADED", "channels": ["slack"], "severity_filter": None},
        {"event_pattern": "*", "channels": ["slack"], "severity_filter": None},
    ]
    with patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=mock_cfg):
        yield mock_cfg


class TestMatchRoutingRules:
    """Tests for the _match_routing_rules function."""

    def test_exact_match(self) -> None:
        rules: list[dict[str, object]] = [
            {
                "event_pattern": "CIRCUIT_BREAKER_OPEN",
                "channels": ["pagerduty", "slack"],
                "severity_filter": "critical",
            },
        ]
        channels, severity = _match_routing_rules("CIRCUIT_BREAKER_OPEN", rules)
        assert channels == {"pagerduty", "slack"}
        assert severity == "critical"

    def test_glob_match(self) -> None:
        rules: list[dict[str, object]] = [
            {
                "event_pattern": "KILL_SWITCH_*",
                "channels": ["pagerduty", "slack"],
                "severity_filter": "critical",
            },
        ]
        channels, severity = _match_routing_rules("KILL_SWITCH_ACTIVATED", rules)
        assert channels == {"pagerduty", "slack"}
        assert severity == "critical"

    def test_wildcard_catch_all(self) -> None:
        rules: list[dict[str, object]] = [
            {
                "event_pattern": "KILL_SWITCH_*",
                "channels": ["pagerduty", "slack"],
                "severity_filter": "critical",
            },
            {"event_pattern": "*", "channels": ["slack"], "severity_filter": None},
        ]
        channels, severity = _match_routing_rules("SOME_OTHER_EVENT", rules)
        assert channels == {"slack"}
        assert severity is None

    def test_first_match_wins(self) -> None:
        rules: list[dict[str, object]] = [
            {
                "event_pattern": "KILL_SWITCH_*",
                "channels": ["pagerduty"],
                "severity_filter": "critical",
            },
            {"event_pattern": "*", "channels": ["slack"], "severity_filter": None},
        ]
        # KILL_SWITCH_ACTIVATED matches the first rule, not the catch-all
        channels, severity = _match_routing_rules("KILL_SWITCH_ACTIVATED", rules)
        assert channels == {"pagerduty"}
        assert severity == "critical"

    def test_no_rules_falls_back_to_slack(self) -> None:
        channels, severity = _match_routing_rules("ANYTHING", [])
        assert channels == {"slack"}
        assert severity is None

    def test_no_severity_filter(self) -> None:
        rules: list[dict[str, object]] = [
            {"event_pattern": "INFO_*", "channels": ["slack"], "severity_filter": None},
        ]
        channels, severity = _match_routing_rules("INFO_UPDATE", rules)
        assert channels == {"slack"}
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


class TestRouteEventSlack:
    """Tests for Slack-only delivery (Telegram retired 2026-06-23)."""

    def test_circuit_breaker_sends_to_pagerduty_and_slack(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        route_event("CIRCUIT_BREAKER_OPEN", {"venue": "binance"})

        mock_pd_send_event.assert_called_once()
        # CIRCUIT_BREAKER_OPEN is a LIVE_ALERT_RULES runtime alert → Slack delivery fires.
        mock_send_uts_live_alert.assert_called_once()

    def test_preflight_failed_sends_to_slack_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"session": "2026-01-01"})

        mock_pd_send_event.assert_not_called()
        # PREFLIGHT_FAILED is a LIVE_ALERT_RULES runtime alert → Slack delivery fires.
        mock_send_uts_live_alert.assert_called_once()

    def test_service_degraded_sends_to_slack_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        route_event("SERVICE_DEGRADED", {"service": "market-data"})

        mock_pd_send_event.assert_not_called()
        # SERVICE_DEGRADED is a LIVE_ALERT_RULES runtime alert → Slack delivery fires.
        mock_send_uts_live_alert.assert_called_once()

    def test_unknown_event_does_not_call_send_uts_live_alert(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        # SOME_OTHER_EVENT is NOT in LIVE_ALERT_RULES → _deliver_to_uts_live_alerts_slack no-op.
        route_event("SOME_OTHER_EVENT", {})

        mock_pd_send_event.assert_not_called()
        mock_send_uts_live_alert.assert_not_called()

    def test_details_forwarded_to_pagerduty(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        details: dict[str, object] = {"venue": "binance", "order_id": "ord-42"}
        route_event("CIRCUIT_BREAKER_OPEN", details)

        pd_kwargs = mock_pd_send_event.call_args.kwargs
        assert pd_kwargs["details"] == details


class TestRouteEventDedup:
    """Tests for deduplication integration."""

    def test_duplicate_event_is_suppressed(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"session": "s1"})
        route_event("PREFLIGHT_FAILED", {"session": "s1"})

        # Only one Slack delivery despite two route_event calls.
        assert mock_send_uts_live_alert.call_count == 1

    def test_different_details_not_suppressed(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"session": "s1"})
        route_event("PREFLIGHT_FAILED", {"session": "s2"})

        assert mock_send_uts_live_alert.call_count == 2


class TestRouteEventFailureHandling:
    def test_pagerduty_failure_is_logged_not_raised(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        mock_pd_send_event.return_value = False
        # Must not raise even when PagerDuty fails.
        route_event("CIRCUIT_BREAKER_OPEN", {})

    def test_slack_failure_is_logged_not_raised(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        mock_send_uts_live_alert.side_effect = RuntimeError("slack down")
        # Must not raise even when Slack delivery fails.
        route_event("PREFLIGHT_FAILED", {})


class TestDeliveryRecordPersistence:
    """Tests that delivery records are persisted via _persist_delivery_record."""

    def test_delivery_record_persisted_on_slack_success(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        route_event("PREFLIGHT_FAILED", {"session": "s1"})

        # One delivery record for slack.
        assert mock_persist_delivery.call_count == 1
        record = mock_persist_delivery.call_args[0][0]
        assert record["channel"] == "slack"
        assert record["status"] == "sent"
        assert "alert_id" in record
        assert record["event_name"] == "PREFLIGHT_FAILED"

    def test_delivery_records_persisted_for_pagerduty_and_slack(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        # KILL_SWITCH_VENUE_DISCONNECT is a real LIVE_ALERT_RULES entry (matches KILL_SWITCH_*).
        route_event("KILL_SWITCH_VENUE_DISCONNECT", {"strategy": "s1"})

        # Two delivery records: pagerduty + slack.
        assert mock_persist_delivery.call_count == 2
        channels = {call[0][0]["channel"] for call in mock_persist_delivery.call_args_list}
        assert channels == {"pagerduty", "slack"}

    def test_failed_delivery_records_have_failed_status(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        mock_send_uts_live_alert.side_effect = RuntimeError("slack down")
        route_event("PREFLIGHT_FAILED", {})

        record = mock_persist_delivery.call_args[0][0]
        assert record["status"] == "failed"


class TestConfigSnapshotPersistence:
    """Tests that config snapshots are persisted."""

    def test_config_snapshot_persisted_on_route(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
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
                "channels": ["pagerduty", "slack"],
                "severity_filter": "critical",
            },
            {
                "event_pattern": "DEFI_HEALTH_FACTOR_CRITICAL",
                "channels": ["pagerduty", "slack"],
                "severity_filter": "critical",
            },
            {
                "event_pattern": "DEFI_WEETH_DEPEG",
                "channels": ["pagerduty", "slack"],
                "severity_filter": "critical",
            },
            {
                "event_pattern": "DEFI_AAVE_UTILIZATION_SPIKE",
                "channels": ["slack"],
                "severity_filter": None,
            },
            {
                "event_pattern": "DEFI_FUNDING_RATE_FLIP",
                "channels": ["slack"],
                "severity_filter": None,
            },
            {
                "event_pattern": "DEFI_FEATURE_STALE",
                "channels": ["slack"],
                "severity_filter": None,
            },
            {"event_pattern": "*", "channels": ["slack"], "severity_filter": None},
        ]

    def test_health_factor_critical_routes_to_pagerduty_and_slack(self) -> None:
        channels, severity = _match_routing_rules("DEFI_HEALTH_FACTOR_CRITICAL", self._defi_rules())
        assert channels == {"pagerduty", "slack"}
        assert severity == "critical"

    def test_weeth_depeg_routes_to_pagerduty_and_slack(self) -> None:
        channels, severity = _match_routing_rules("DEFI_WEETH_DEPEG", self._defi_rules())
        assert channels == {"pagerduty", "slack"}
        assert severity == "critical"

    def test_aave_utilization_routes_to_slack_only(self) -> None:
        channels, severity = _match_routing_rules("DEFI_AAVE_UTILIZATION_SPIKE", self._defi_rules())
        assert channels == {"slack"}
        assert severity is None

    def test_funding_rate_flip_routes_to_slack_only(self) -> None:
        channels, severity = _match_routing_rules("DEFI_FUNDING_RATE_FLIP", self._defi_rules())
        assert channels == {"slack"}
        assert severity is None

    def test_feature_stale_routes_to_slack_only(self) -> None:
        channels, severity = _match_routing_rules("DEFI_FEATURE_STALE", self._defi_rules())
        assert channels == {"slack"}
        assert severity is None


class TestCustomRoutingRules:
    """Tests with custom routing rules."""

    def test_custom_rule_routes_to_pagerduty_only(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        mock_cfg = MagicMock()
        mock_cfg.uts_live_alerts_slack_webhook = ""
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
        # pagerduty only channel → _deliver_to_uts_live_alerts_slack never invoked.
        mock_send_uts_live_alert.assert_not_called()


class TestUtsLiveAlertsSlack:
    """Tests for _deliver_to_uts_live_alerts_slack — the primary Slack delivery path."""

    @pytest.fixture
    def mock_config_with_webhook(self):
        """AlertingSystemConfig with a configured Slack webhook."""
        from alerting_service.notifiers.router import _get_cloud_config

        _get_cloud_config.cache_clear()
        mock_cfg = MagicMock()
        mock_cfg.uts_live_alerts_slack_webhook = "https://hooks.slack.com/services/T/B/xyz"
        mock_cfg.data_pipeline_slack_webhook = ""
        mock_cfg.gcp_project_id = "test-project"
        mock_cfg.pagerduty_disabled = False
        mock_cfg.quietness_baseline_mode = False
        mock_cfg.routing_rules = [
            {"event_pattern": "*", "channels": ["slack"], "severity_filter": None},
        ]
        with patch("alerting_service.notifiers.router.AlertingSystemConfig", return_value=mock_cfg):
            yield mock_cfg
        _get_cloud_config.cache_clear()

    def test_runtime_alert_is_delivered_to_slack(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        mock_config_with_webhook: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        # PREFLIGHT_FAILED matches a LIVE_ALERT_RULES pattern → runtime alert → delivered.
        route_event("PREFLIGHT_FAILED", {"message": "venue auth ping failed", "venue": "binance"})

        mock_send_uts_live_alert.assert_called_once()
        args = mock_send_uts_live_alert.call_args.args
        assert args[0] == "https://hooks.slack.com/services/T/B/xyz"
        assert args[1] == "PREFLIGHT_FAILED"

    def test_non_runtime_alert_is_not_delivered(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        mock_config_with_webhook: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        # INTERNAL_QG_HEALTH is NOT in LIVE_ALERT_RULES → no-op, send_uts_live_alert not called.
        route_event("INTERNAL_QG_HEALTH", {"check": "ping"})

        mock_send_uts_live_alert.assert_not_called()

    def test_sm_webhook_takes_precedence_over_config(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        mock_config_with_webhook: MagicMock,
    ) -> None:
        with patch(
            "alerting_service.notifiers.router.get_paging_credentials",
            return_value={"uts_live_alerts_slack_webhook": "https://hooks.slack.com/services/SM/HOOK"},
        ):
            route_event("PREFLIGHT_FAILED", {"message": "x"})

        mock_send_uts_live_alert.assert_called_once()
        assert mock_send_uts_live_alert.call_args.args[0] == "https://hooks.slack.com/services/SM/HOOK"

    def test_send_failure_is_recorded_as_failed_and_does_not_raise(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        mock_config_with_webhook: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        # A real send failure (exception) → delivery record says "failed", no raise.
        mock_send_uts_live_alert.side_effect = RuntimeError("slack down")
        route_event("PREFLIGHT_FAILED", {"message": "x"})

        # Delivery record must show "failed" status.
        records = [call[0][0] for call in mock_persist_delivery.call_args_list]
        slack_records = [r for r in records if r.get("channel") == "slack"]
        assert len(slack_records) == 1
        assert slack_records[0]["status"] == "failed"


# ── 2026-06-24: recurring-WARN dedup cooldown window ─────────────────────────
# ── 2026-07-15: generalized to cover opted-in static/CRITICAL conditions too ─
class TestRecurringWarnDedupWindow:
    def test_recurring_warn_events_get_30min_cooldown(self) -> None:
        from alerting_service.notifiers.router import _dedup_window_for

        assert _dedup_window_for("DP_VM_STALL") == 1800.0
        assert _dedup_window_for("DP_EVENT_LOOP_STARVED") == 1800.0

    def test_recurring_critical_event_gets_30min_cooldown(self) -> None:
        """DP_RUN_MOSTLY_EMPTY (CRITICAL) is a static, re-scanned-every-tick
        manifest-cell signal — it opts into the recurring cooldown so the ~15-min
        meta-sweep cadence collapses to one delivered alert per window, while still
        re-nagging (paging) every 30 min while the condition remains unresolved."""
        from alerting_service.notifiers.router import _dedup_window_for

        assert _dedup_window_for("DP_RUN_MOSTLY_EMPTY") == 1800.0

    def test_non_recurring_events_use_default_window(self) -> None:
        from alerting_service.notifiers.router import _dedup_window_for

        # CRITICAL one-shot/flappy events keep the short default (None → 60s) so
        # their page is not over-suppressed.
        assert _dedup_window_for("DP_VM_GONE_NO_CAPTURE") is None
        assert _dedup_window_for("CONSOLIDATOR_DOWN") is None
        assert _dedup_window_for("KILL_SWITCH_ACTIVATED") is None
