"""Configuration for alerting-system."""

from unified_config_interface import UnifiedCloudConfig


class AlertingSystemConfig(UnifiedCloudConfig):
    """Configuration for alerting-service."""

    service_name: str = "alerting-service"
