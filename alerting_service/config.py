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
        # ── T1 CRITICAL — PagerDuty P1 + Telegram ────────────────────────
        # Kill switch (manual or automatic)
        {
            "event_pattern": "KILL_SWITCH_*",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        # Circuit breaker venue blocked
        {
            "event_pattern": "CIRCUIT_BREAKER_OPEN",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        # Multi-leg partial fill — unhedged position
        {
            "event_pattern": "UNHEDGED_POSITION_ALERT",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        # Compensation trade failed — unhedged + circuit breaker
        {
            "event_pattern": "MULTI_LEG_COMPENSATION_FAILED",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        # Dual failure — can't reconcile AND can't execute
        {
            "event_pattern": "DUAL_FAILURE_DETECTED",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        # Order recovery failed — orphaned orders unresolvable
        {
            "event_pattern": "ORDER_RECOVERY_FAILED",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        # Service-level critical error
        {
            "event_pattern": "SERVICE_ERROR_CRITICAL",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        # DeFi P0 — health factor emergency, depeg
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
        # Unified margin-event ladder (PBM is canonical producer; thresholds
        # come from UAC LIQUIDATION_PARAMS_REGISTRY). Replaces the prior
        # service-local re-derivation of HF in risk / strategy / PBM.
        {
            "event_pattern": "MARGIN_LIQUIDATION",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        {
            "event_pattern": "MARGIN_CRITICAL",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        # ── T2 HIGH — PagerDuty P2 (warning) + Telegram ──────────────────
        # Circuit breaker backoff escalating (repeated recovery failure)
        {
            "event_pattern": "CIRCUIT_BREAKER_BACKOFF_*",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "warning",
        },
        # Orphaned order found during startup recovery
        {
            "event_pattern": "ORDER_ORPHANED",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "warning",
        },
        # Position drift detected (could be WARNING or CRITICAL severity)
        {
            "event_pattern": "POSITION_DRIFT_DETECTED",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "warning",
        },
        # Closing positions without verified reconciliation state
        {
            "event_pattern": "RECON_DEGRADED_*",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "warning",
        },
        # Position discrepancy large enough to escalate
        {
            "event_pattern": "POSITION_CRITICAL_DISCREPANCY",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "warning",
        },
        # Margin warning band -- PagerDuty P2 so on-call sees drift before
        # critical, but no immediate auto-action required.
        {
            "event_pattern": "MARGIN_WARNING",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "warning",
        },
        # ── T3 WARNING — Telegram only ───────────────────────────────────
        # Circuit breaker throttling (venue health declining)
        {
            "event_pattern": "CIRCUIT_BREAKER_DEGRADED",
            "channels": ["telegram"],
            "severity_filter": None,
        },
        # Circuit breaker recovered
        {
            "event_pattern": "CIRCUIT_BREAKER_CLOSED",
            "channels": ["telegram"],
            "severity_filter": None,
        },
        # Service-level non-critical error
        {
            "event_pattern": "SERVICE_ERROR",
            "channels": ["telegram"],
            "severity_filter": None,
        },
        # Preflight check failed (order rejected before submission)
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
        # Auto-correction dispatched by reconciliation
        {
            "event_pattern": "POSITION_CORRECTION_*",
            "channels": ["telegram"],
            "severity_filter": None,
        },
        # Portfolio rebalancing triggered by drift
        {
            "event_pattern": "PORTFOLIO_REBALANCE_*",
            "channels": ["telegram"],
            "severity_filter": None,
        },
        # Order recovery lifecycle (initiated, completed)
        {
            "event_pattern": "ORDER_RECOVERY_*",
            "channels": ["telegram"],
            "severity_filter": None,
        },
        # DeFi monitoring alerts (non-critical)
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
        # ── T4 INFO — Telegram fallback (everything else) ────────────────
        # Catches all events not matched above. Nothing is silent.
        {
            "event_pattern": "*",
            "channels": ["telegram"],
            "severity_filter": None,
        },
    ]


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
