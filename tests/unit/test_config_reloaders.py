"""Tests for alerting-service config_reloaders hot-reload callbacks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestConfigReloadersCallbacks:
    """Verify callback atomic-swap and getter semantics."""

    def test_on_instruments_reload_updates_snapshot(self) -> None:
        import alerting_service.config_reloaders as cr

        mock_config = MagicMock()
        mock_config.subscription_list = ["BTC-USDT"]
        mock_config.enabled_venues = ["BINANCE"]
        with patch("alerting_service.config_reloaders.log_event"):
            cr._on_instruments_reload(mock_config)
        assert cr._active_instruments is mock_config

    def test_get_active_instruments_returns_snapshot(self) -> None:
        import alerting_service.config_reloaders as cr

        mock_config = MagicMock()
        cr._active_instruments = mock_config
        assert cr.get_active_instruments() is mock_config

    def test_on_venues_reload_updates_snapshot(self) -> None:
        import alerting_service.config_reloaders as cr

        mock_config = MagicMock()
        mock_config.enabled_venues = ["BINANCE", "CME"]
        with patch("alerting_service.config_reloaders.log_event"):
            cr._on_venues_reload(mock_config)
        assert cr._active_venues is mock_config

    def test_get_active_venues_returns_snapshot(self) -> None:
        import alerting_service.config_reloaders as cr

        mock_config = MagicMock()
        cr._active_venues = mock_config
        assert cr.get_active_venues() is mock_config

    def test_on_alert_rules_reload_updates_snapshot(self) -> None:
        import alerting_service.config_reloaders as cr

        mock_config = MagicMock()
        mock_config.rules = [{"name": "rule_1"}]
        mock_config.service_overrides = {"svc": "override"}
        with patch("alerting_service.config_reloaders.log_event"):
            cr._on_alert_rules_reload(mock_config)
        assert cr._active_alert_rules is mock_config

    def test_get_active_alert_rules_returns_snapshot(self) -> None:
        import alerting_service.config_reloaders as cr

        mock_config = MagicMock()
        cr._active_alert_rules = mock_config
        assert cr.get_active_alert_rules() is mock_config

    def test_start_skips_when_no_bucket(self) -> None:
        import alerting_service.config_reloaders as cr

        mock_cfg = type("C", (), {"config_store_bucket": "", "project_id": None})()
        cr.start_domain_config_reloaders(mock_cfg)

    def test_stop_when_none(self) -> None:
        import alerting_service.config_reloaders as cr

        cr._instrument_reloader = None
        cr._venue_reloader = None
        cr._alert_rule_reloader = None
        cr.stop_domain_config_reloaders()
        assert cr._instrument_reloader is None
        assert cr._venue_reloader is None
        assert cr._alert_rule_reloader is None

    def test_stop_calls_stop_watching(self) -> None:
        import alerting_service.config_reloaders as cr

        mock_inst = MagicMock()
        mock_venue = MagicMock()
        mock_alert = MagicMock()
        cr._instrument_reloader = mock_inst
        cr._venue_reloader = mock_venue
        cr._alert_rule_reloader = mock_alert
        cr.stop_domain_config_reloaders()
        mock_inst.stop_watching.assert_called_once()
        mock_venue.stop_watching.assert_called_once()
        mock_alert.stop_watching.assert_called_once()
        assert cr._instrument_reloader is None
        assert cr._venue_reloader is None
        assert cr._alert_rule_reloader is None
