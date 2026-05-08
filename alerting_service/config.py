"""
Configuration for alerting-service
"""

from typing import ClassVar

from pydantic import Field
from unified_api_contracts import LIVE_ALERT_RULES
from unified_trading_library import UnifiedCloudConfig


def _default_routing_rules() -> list[dict[str, object]]:
    """Return the default routing rules — single SSOT consumption from UAC.

    Phase 2 of `alerting_service_live_rules_2026_05_07` migrated the
    previously-inlined ~28 routing rules to ``LIVE_ALERT_RULES`` in
    ``unified_api_contracts.alerting``. Each rule specifies:

      - event_pattern: fnmatch pattern matched against event names
      - channels: list of notifier channel names to deliver to
      - severity_filter: optional PagerDuty severity override (None = channel default)

    The UAC closed-set taxonomy enforces fail-loud construction (unknown
    AlertCode / threshold_key / KILL_SWITCH-flag-on-non-KILL_SWITCH-code) so
    drift between this service and the UAC SSOT cannot creep back.

    Plan: `unified-trading-pm/plans/active/alerting_service_live_rules_2026_05_07.md`
    Phase 2.

    Operator overrides via the `routing_rules` field on
    ``AlertingSystemConfig`` continue to work; this default-factory only
    seeds the initial value.
    """
    return [rule.to_routing_dict() for rule in LIVE_ALERT_RULES]


class AlertingSystemConfig(UnifiedCloudConfig):
    """Configuration for alerting-service"""

    service_name: str = "alerting-service"
    config_store_bucket: str = Field(
        default="",
        description="Cloud storage bucket for domain config store (hot reload)",
    )
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
