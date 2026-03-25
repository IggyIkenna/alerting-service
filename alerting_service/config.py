"""
Configuration for alerting-service
"""

from typing import ClassVar

from pydantic import Field
from unified_trading_library import UnifiedCloudConfig


def _default_routing_rules() -> list[dict[str, object]]:
    """Return the default routing rules matching the legacy hardcoded behavior.

    Each rule specifies:
      - event_pattern: glob-style pattern matched against event names (fnmatch)
      - channels: list of notifier channel names to deliver to
      - severity_filter: optional PagerDuty severity override (null = channel default)
    """
    return [
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
        # SERVICE_ERROR with critical severity -> PagerDuty + Telegram
        {
            "event_pattern": "SERVICE_ERROR_CRITICAL",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        # SERVICE_ERROR (non-critical) -> Telegram only
        {
            "event_pattern": "SERVICE_ERROR",
            "channels": ["telegram"],
            "severity_filter": None,
        },
        # DeFi P0 alerts — immediate PagerDuty + Telegram
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
        # DeFi P1 alerts — Telegram only
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
        {
            "event_pattern": "PREFLIGHT_FAILED",
            "channels": ["telegram"],
            "severity_filter": None,
        },
        {
            "event_pattern": "SERVICE_DEGRADED",
            "channels": ["telegram"],
            "severity_filter": None,
        },
        {
            "event_pattern": "*",
            "channels": ["telegram"],
            "severity_filter": None,
        },
    ]


class AlertingSystemConfig(UnifiedCloudConfig):
    """Configuration for alerting-service"""

    service_name: str = "alerting-service"
    slack_webhook_url: str = Field(default="", description="Slack incoming webhook URL")
    telegram_bot_token: str = Field(default="", description="Telegram Bot API token")
    telegram_chat_id: str = Field(default="", description="Telegram chat ID for alerts")
    pagerduty_routing_key: str | None = None
    routing_rules: list[dict[str, object]] = Field(default_factory=_default_routing_rules)
    email_smtp_host: str | None = None
    email_smtp_port: int = 587
    email_to: ClassVar[list[str]] = []
    google_oauth_domain: str = ""
    anthropic_api_key: str | None = None
    poll_interval_seconds: int = 10
    metrics_endpoints: ClassVar[dict[str, str]] = {}
