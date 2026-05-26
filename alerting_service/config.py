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
    telegram_chat_id_ops: str = Field(
        default="",
        description="Telegram chat ID for live-ops runtime alerts (ops team channel). "
        "LIVE_ALERT_RULES events route here when set; CI/QG events use telegram_chat_id.",
    )
    pagerduty_routing_key: str | None = None
    # ── Layer-3 Twilio voice + SMS fallback (added 2026-05-23) ────────────
    # Per `plans/active/independent_fallback_twilio_voice_2026_05_23.md` Phase 1.
    # Operator-only: account creation + SM push (see `plans/active/_agent_pings.md`
    # 2026-05-23 ikenna-slot-1 → operator item #1).
    twilio_account_sid: str = Field(default="", description="Twilio account SID (from SM)")
    twilio_auth_token: str = Field(
        default="",
        description=(
            "Twilio auth token (from SM). NEVER LOG — httpx silenced in notifiers/twilio_voice.py + twilio_sms.py."
        ),
    )
    twilio_from_number: str = Field(default="", description="Twilio-owned voice-capable phone number (E.164)")
    twilio_to_number_primary: str = Field(default="", description="Primary on-call mobile (E.164)")
    twilio_to_number_secondary: str = Field(default="", description="Secondary on-call mobile (E.164)")
    twilio_to_number_founder: str = Field(default="", description="Founder mobile (E.164) for SLA-breach escalation")
    # ── Layer-4 physical pager (added 2026-05-23) ─────────────────────────
    # Per `plans/active/physical_pager_research_and_webhook_prototype_2026_05_23.md`.
    # Operator buys device + populates SM (see ping doc item #2). Until then,
    # vendor_name is empty + the notifier no-ops with a warning log.
    physical_pager_vendor_name: str = Field(
        default="",
        description=("Closed set: WEBHOOK | GSM_SIREN (see notifiers/physical_pager.py). Empty = no-op."),
    )
    physical_pager_endpoint_url: str = Field(
        default="", description="Vendor-specific webhook URL or SMS-trigger endpoint"
    )
    physical_pager_auth_header: str = Field(default="", description="Optional auth header value")
    physical_pager_to_number: str = Field(default="", description="For SMS-trigger devices (e.g. GSM_SIREN)")
    routing_rules: list[dict[str, object]] = Field(default_factory=_default_routing_rules)
    quietness_baseline_mode: bool = Field(
        default=False,
        description="Phase 7 quietness baseline — 48h staging noise run with PagerDuty disabled",
    )
    pagerduty_disabled: bool = Field(
        default=False,
        description="Disable PagerDuty delivery (set true for quietness baseline or staging runs)",
    )
    run_duration_hours: int = Field(
        default=0,
        description="Auto-shutdown after N hours (0 = run indefinitely until SIGTERM)",
    )
    email_smtp_host: str | None = None
    email_smtp_port: int = 587
    email_to: ClassVar[list[str]] = []
    google_oauth_domain: str = ""
    anthropic_api_key: str | None = None
    poll_interval_seconds: int = 10
    metrics_endpoints: ClassVar[dict[str, str]] = {}
