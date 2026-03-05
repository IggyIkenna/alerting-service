"""
Configuration for alerting-service
"""

from typing import ClassVar

from pydantic import Field
from unified_config_interface import UnifiedCloudConfig


class AlertingSystemConfig(UnifiedCloudConfig):
    """Configuration for alerting-service"""

    service_name: str = "alerting-service"
    slack_webhook_url: str = Field(default="", description="Slack incoming webhook URL")
    pagerduty_routing_key: str | None = None
    email_smtp_host: str | None = None
    email_smtp_port: int = 587
    email_to: ClassVar[list[str]] = []
    google_oauth_domain: str = ""
    anthropic_api_key: str | None = None
    poll_interval_seconds: int = 10
    metrics_endpoints: ClassVar[dict[str, str]] = {}
