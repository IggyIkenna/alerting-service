"""Unit tests for the event router."""

from unittest.mock import MagicMock, patch

import pytest

from alerting_service.notifiers.router import (
    _build_delivery_record,
    _extract_deployment_target,
    _match_routing_rules,
    _persisted_severity,
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


class TestDeliveryRecordEnrichedFields:
    """deployment_alerts_ingestion_completeness_2026_07_20.md todo 2 — a delivery record must not
    be detail-poorer than the decision record: alert_class/severity/message/service/
    deployment_target ride alongside channel/status/response_detail."""

    def test_build_delivery_record_carries_alert_class_and_enriched_fields(self) -> None:
        record = _build_delivery_record(
            "alert-1",
            "slack",
            "sent",
            "ok",
            "PREFLIGHT_FAILED",
            severity="warning",
            message="preflight check failed",
            service="market-tick-data-service",
            deployment_target="cefi-backfill-20260620",
        )
        assert record["alert_class"] == "PREFLIGHT_FAILED"  # alert_class == event_name
        assert record["severity"] == "warning"
        assert record["message"] == "preflight check failed"
        assert record["service"] == "market-tick-data-service"
        assert record["deployment_target"] == "cefi-backfill-20260620"
        # Delivery-status fields unchanged.
        assert record["channel"] == "slack"
        assert record["status"] == "sent"

    def test_build_delivery_record_enriched_fields_default_none(self) -> None:
        # An emit site that provides none of the optional fields -> honest None, never fabricated.
        record = _build_delivery_record("alert-1", "slack", "sent", "ok", "PREFLIGHT_FAILED")
        assert record["severity"] is None
        assert record["message"] is None
        assert record["service"] is None
        assert record["deployment_target"] is None

    def test_persisted_severity_prefers_pagerduty_normalised_tier(self) -> None:
        assert _persisted_severity("critical", {"severity": "WARNING"}) == "critical"

    def test_persisted_severity_falls_back_to_details_severity(self) -> None:
        assert _persisted_severity(None, {"severity": "WARNING"}) == "WARNING"

    def test_persisted_severity_none_when_neither_present(self) -> None:
        assert _persisted_severity(None, {}) is None

    def test_extract_deployment_target_reads_vm_name(self) -> None:
        assert _extract_deployment_target({"vm_name": "cefi-backfill-20260620"}) == "cefi-backfill-20260620"

    def test_extract_deployment_target_reads_vm_fallback(self) -> None:
        assert _extract_deployment_target({"vm": "cefi-backfill-20260620"}) == "cefi-backfill-20260620"

    def test_extract_deployment_target_reads_deployment_id_fallback(self) -> None:
        assert _extract_deployment_target({"deployment_id": "dep-123"}) == "dep-123"

    def test_extract_deployment_target_none_when_absent(self) -> None:
        assert _extract_deployment_target({"session": "s1"}) is None

    def test_route_event_end_to_end_populates_enriched_fields_on_the_persisted_record(
        self,
        mock_pd_send_event: MagicMock,
        mock_send_uts_live_alert: MagicMock,
        mock_log_event: MagicMock,
        mock_config_slack_only: MagicMock,
        mock_persist_delivery: MagicMock,
        mock_persist_config: MagicMock,
        empty_paging_creds: MagicMock,
    ) -> None:
        # Emit site provides vm_name + source in details -> both land on the persisted record,
        # proving the enrichment is wired end-to-end, not just testable in isolation. (details'
        # "source" key feeds the delivery record's "service" field — see _record_batch_audit /
        # _deliver_to_channels: source = str(details.get("source", "alerting-service")).)
        route_event("PREFLIGHT_FAILED", {"vm_name": "cefi-backfill-20260620", "source": "market-tick-data-service"})

        record = mock_persist_delivery.call_args[0][0]
        assert record["alert_class"] == "PREFLIGHT_FAILED"
        assert record["deployment_target"] == "cefi-backfill-20260620"
        assert record["service"] == "market-tick-data-service"


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

    def test_dp_vm_gone_no_capture_gets_30min_cooldown(self) -> None:
        """DP_VM_GONE_NO_CAPTURE (CRITICAL) is a static, re-scanned-every-tick
        exit-code-sweep signal (>= 300s detector cadence) — it opts into the
        recurring cooldown the same way DP_RUN_MOSTLY_EMPTY does, so it re-nags
        every 30 min while unresolved instead of paging on every sweep."""
        from alerting_service.notifiers.router import _dedup_window_for

        assert _dedup_window_for("DP_VM_GONE_NO_CAPTURE") == 1800.0

    def test_dp_vm_gone_no_capture_collapses_within_window(self) -> None:
        """Repeated route_event calls for the SAME identity within the 30-min
        cooldown collapse to ONE delivered alert (mirrors TestTtlOverride's
        collapse/re-nag pattern in test_dedup.py, exercised via the router's
        actual _dedup_window_for lookup instead of a hardcoded override)."""
        from alerting_service.core.dedup import AlertDeduplicator
        from alerting_service.notifiers.router import _dedup_window_for

        cooldown = _dedup_window_for("DP_VM_GONE_NO_CAPTURE")
        assert cooldown is not None
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        det = {"vm_name": "vm-dp-vm-gone-1"}
        with patch("alerting_service.core.dedup.time.monotonic", return_value=0.0):
            assert dedup.is_duplicate("DP_VM_GONE_NO_CAPTURE", det, ttl_override=cooldown) is False
        # at t=300s (one detector sweep later) the SAME condition is still within
        # the 1800s cooldown window -> collapses, no second alert delivered.
        with patch("alerting_service.core.dedup.time.monotonic", return_value=300.0):
            assert dedup.is_duplicate("DP_VM_GONE_NO_CAPTURE", det, ttl_override=cooldown) is True

    def test_dp_vm_gone_no_capture_re_nags_past_boundary(self) -> None:
        """Past the 30-min cooldown window, an unresolved DP_VM_GONE_NO_CAPTURE
        re-fires (re-nags) rather than staying silently suppressed forever."""
        from alerting_service.core.dedup import AlertDeduplicator
        from alerting_service.notifiers.router import _dedup_window_for

        cooldown = _dedup_window_for("DP_VM_GONE_NO_CAPTURE")
        assert cooldown is not None
        dedup = AlertDeduplicator(ttl_seconds=60.0)
        det = {"vm_name": "vm-dp-vm-gone-2"}
        with patch("alerting_service.core.dedup.time.monotonic", return_value=0.0):
            assert dedup.is_duplicate("DP_VM_GONE_NO_CAPTURE", det, ttl_override=cooldown) is False
        with patch("alerting_service.core.dedup.time.monotonic", return_value=cooldown + 1.0):
            assert dedup.is_duplicate("DP_VM_GONE_NO_CAPTURE", det, ttl_override=cooldown) is False

    def test_non_recurring_events_use_default_window(self) -> None:
        from alerting_service.notifiers.router import _dedup_window_for

        # CRITICAL one-shot/flappy events keep the short default (None → 60s) so
        # their page is not over-suppressed.
        assert _dedup_window_for("CONSOLIDATOR_DOWN") is None
        assert _dedup_window_for("KILL_SWITCH_ACTIVATED") is None


# ── 2026-07-28: STATIC BACKLOG DP_RUN_MOSTLY_EMPTY paging-cadence downgrade ──
# cefi_high_attempted_failed_batch_cluster_2026_07_23.md [BACKEND] P2 ruling.
class TestStaticBacklogDpRunMostlyEmptyCooldown:
    def test_static_backlog_cell_gets_daily_cooldown(self) -> None:
        from alerting_service.notifiers.dp_run_mostly_empty_static_backlog import (
            STATIC_BACKLOG_COOLDOWN_SECONDS,
        )
        from alerting_service.notifiers.router import _dedup_window_for

        assert STATIC_BACKLOG_COOLDOWN_SECONDS == 86400.0
        assert _dedup_window_for("DP_RUN_MOSTLY_EMPTY", {"is_static_backlog": True}) == STATIC_BACKLOG_COOLDOWN_SECONDS

    def test_fresh_cell_keeps_30min_cooldown(self) -> None:
        from alerting_service.notifiers.router import _dedup_window_for

        assert _dedup_window_for("DP_RUN_MOSTLY_EMPTY", {"is_static_backlog": False}) == 1800.0
        assert _dedup_window_for("DP_RUN_MOSTLY_EMPTY", {}) == 1800.0

    def test_no_details_arg_stays_back_compat(self) -> None:
        """Callers that only know the event name (existing call sites, tests)
        still get the normal 30-min cooldown — the downgrade requires details."""
        from alerting_service.notifiers.router import _dedup_window_for

        assert _dedup_window_for("DP_RUN_MOSTLY_EMPTY") == 1800.0

    def test_other_events_unaffected_by_is_static_backlog(self) -> None:
        from alerting_service.notifiers.router import _dedup_window_for

        assert _dedup_window_for("DP_VM_GONE_NO_CAPTURE", {"is_static_backlog": True}) == 1800.0


class TestEffectiveDpSeverity:
    def test_static_backlog_downgrades_critical_to_warn(self) -> None:
        from unified_api_contracts import AlertSeverity

        from alerting_service.notifiers.router import _effective_dp_severity

        result = _effective_dp_severity(
            "DP_RUN_MOSTLY_EMPTY",
            {"is_static_backlog": True},
            AlertSeverity.CRITICAL,
        )
        assert result is AlertSeverity.WARN

    def test_fresh_cell_stays_critical(self) -> None:
        from unified_api_contracts import AlertSeverity

        from alerting_service.notifiers.router import _effective_dp_severity

        assert (
            _effective_dp_severity("DP_RUN_MOSTLY_EMPTY", {"is_static_backlog": False}, AlertSeverity.CRITICAL)
            is AlertSeverity.CRITICAL
        )
        assert _effective_dp_severity("DP_RUN_MOSTLY_EMPTY", {}, AlertSeverity.CRITICAL) is AlertSeverity.CRITICAL

    def test_other_events_never_downgraded(self) -> None:
        from unified_api_contracts import AlertSeverity

        from alerting_service.notifiers.router import _effective_dp_severity

        assert (
            _effective_dp_severity("DP_VM_GONE_NO_CAPTURE", {"is_static_backlog": True}, AlertSeverity.CRITICAL)
            is AlertSeverity.CRITICAL
        )

    def test_non_critical_severity_passthrough(self) -> None:
        from unified_api_contracts import AlertSeverity

        from alerting_service.notifiers.router import _effective_dp_severity

        assert (
            _effective_dp_severity("DP_RUN_MOSTLY_EMPTY", {"is_static_backlog": True}, AlertSeverity.INFO)
            is AlertSeverity.INFO
        )
