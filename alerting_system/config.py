"""
Configuration for alerting-system
"""

from unified_config_interface import UnifiedCloudConfig


class AlertingSystemConfig(UnifiedCloudConfig):
    """Configuration for alerting-system"""

    service_name: str = "alerting-system"

    # Add service-specific config fields here
