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
    uts_live_alerts_slack_webhook: str = Field(
        default="",
        description=(
            "Slack Incoming Webhook for the #uts-live-alerts channel. Live-ops runtime "
            "alerts (LIVE_ALERT_RULES) delivered to the Telegram 'UTS Live Alerts' group "
            "are mirrored here. Same agent-orchestrator-alerts Slack app. "
            "SM hot-reload via 'alerting-uts-live-alerts-slack-webhook'; env "
            "UTS_LIVE_ALERTS_SLACK_WEBHOOK is the fallback. Empty = mirror disabled."
        ),
    )
    data_pipeline_slack_webhook: str = Field(
        default="",
        description=(
            "Slack Incoming Webhook for the #data-pipeline-alerts channel. Data-pipeline "
            "self-monitoring alerts (the DP_* family + CONSOLIDATOR_DOWN, matched by UAC "
            "DATA_PIPELINE_ALERT_RULES) are mirrored here; CRITICAL ones ALSO page via the "
            "incident path. SM hot-reload via 'DATA_PIPELINE_ALERTS_SLACK_WEBHOOK'; env "
            "DATA_PIPELINE_ALERTS_SLACK_WEBHOOK is the fallback. Empty = mirror disabled."
        ),
    )
    deployment_ui_base_url: str = Field(
        default="",
        description=(
            "Base URL of the deployment-ui (e.g. https://deployment.odum-research.com). Used to "
            "build click-through deep-links in #data-pipeline-alerts / deployment alerts — VM "
            "logs ({base}/ops/vms/{vm}), deployment detail ({base}/deployments/{name}), data "
            "status ({base}/service/{svc}/data-status). SM hot-reload via 'DEPLOYMENT_UI_BASE_URL'; "
            "env DEPLOYMENT_UI_BASE_URL is the fallback. Empty = links omitted (no broken link)."
        ),
    )
    deployment_scripts_log_bucket: str = Field(
        default="",
        description=(
            "GCS bucket holding the durable per-VM run.log (gs://{bucket}/vm-logs/{vm}/run.log). "
            "Used to build a GCS console link in alert actions. SM hot-reload via "
            "'DEPLOYMENT_SCRIPTS_LOG_BUCKET'; env DEPLOYMENT_SCRIPTS_LOG_BUCKET is the fallback. "
            "Empty = run.log console link omitted."
        ),
    )
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
    email_smtp_username: str = Field(
        default="",
        description=(
            "SMTP auth username for the CRITICAL-severity email fallback (fires when "
            "PagerDuty is unavailable/fails — see notifiers/email.py). SM hot-reload via "
            "'alerting-email-smtp-username'; env EMAIL_SMTP_USERNAME is the fallback. "
            "Empty = email fallback disabled."
        ),
    )
    email_smtp_password: str = Field(
        default="",
        description=(
            "SMTP auth password (from SM). NEVER LOG. SM hot-reload via "
            "'alerting-email-smtp-password'; env EMAIL_SMTP_PASSWORD is the fallback."
        ),
    )
    email_from_address: str = Field(
        default="",
        description=(
            "From: address for the CRITICAL email fallback. SM hot-reload via "
            "'alerting-email-from-address'; env EMAIL_FROM_ADDRESS is the fallback. Empty "
            "falls back to email_smtp_username at send time."
        ),
    )
    email_to: list[str] = Field(
        default_factory=list,
        description=(
            "To: recipient addresses for the CRITICAL email fallback (2026-08-06 fix — "
            "was a dead ClassVar with zero consumers; now wired to notifiers/email.py). "
            "Empty = email fallback disabled (no recipients)."
        ),
    )
    google_oauth_domain: str = ""
    poll_interval_seconds: int = 10
    metrics_endpoints: ClassVar[dict[str, str]] = {}
    execution_service_health_url: str = Field(
        default="",
        description=(
            "Health-check URL for execution-service (e.g. "
            "https://execution-service-xxx.a.run.app/health). Probed by "
            "dependency_health_runner.py for the execution_service_health "
            "dependency policy (kill_switch_scope=GLOBAL on SEV0). Env "
            "EXECUTION_SERVICE_HEALTH_URL. Empty = probe fails open (reports "
            "healthy) — same fail-open default as every unconfigured "
            "dependency probe today."
        ),
    )
    strategy_service_health_url: str = Field(
        default="",
        description=(
            "Health-check URL for strategy-service (e.g. "
            "https://strategy-service-xxx.a.run.app/health). Probed by "
            "dependency_health_runner.py for the strategy_service_health "
            "dependency policy (kill_switch_scope=STRATEGY on SEV0). Env "
            "STRATEGY_SERVICE_HEALTH_URL. Empty = probe fails open (reports "
            "healthy)."
        ),
    )
    run_subscriber_in_api: bool = Field(
        default=False,
        description=(
            "When true, the FastAPI app (api/main.py) ALSO runs the live AlertSubscriber "
            "pull-loop in a background task via lifespan. This makes a single Cloud Run "
            "service both serve $PORT (startup probe / health) AND consume PubSub "
            "(lifecycle-events-sub etc.) — the always-on durable subscriber that replaces "
            "the fragile batch-VM (stall-watchdog) deployment. Env: RUN_SUBSCRIBER_IN_API. "
            "SSOT: plans/active/issues/dp_event_pubsub_delivery_gap_2026_06_22.md."
        ),
    )
