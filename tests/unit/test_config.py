"""Unit tests for alerting-service config."""

from alerting_service.config import AlertingSystemConfig


def test_config_service_name() -> None:
    """Test that config loads with correct service name."""
    config = AlertingSystemConfig()
    assert config.service_name == "alerting-service"
