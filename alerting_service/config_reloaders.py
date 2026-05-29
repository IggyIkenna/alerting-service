"""Domain config hot-reload wiring for alerting-service."""

from __future__ import annotations

import logging
import threading

from unified_trading_library import (
    AlertRuleDomainConfig,
    DomainConfigReloader,
    InstrumentDomainConfig,
    SecretManagerClient,
    VenueDomainConfig,
    log_event,
)

from alerting_service.config import AlertingSystemConfig

logger = logging.getLogger(__name__)

# ── Paging credentials SM secret names ───────────────────────────────────────
_SM_BOT_TOKEN = "alerting-telegram-bot-token"
_SM_CHAT_ID = "alerting-telegram-chat-id"
_SM_CHAT_ID_OPS = "alerting-telegram-chat-id-ops"
# Twilio voice/SMS fallback (Layer-3) — pushed by operator per ping doc item #1
_SM_TWILIO_ACCOUNT_SID = "alerting-twilio-account-sid"
_SM_TWILIO_AUTH_TOKEN = "alerting-twilio-auth-token"
_SM_TWILIO_FROM_NUMBER = "alerting-twilio-from-number"
_SM_TWILIO_TO_PRIMARY = "alerting-twilio-to-number-primary"
_SM_TWILIO_TO_SECONDARY = "alerting-twilio-to-number-secondary"
_SM_TWILIO_TO_FOUNDER = "alerting-twilio-to-number-founder"
# UTS Live Alerts → Slack mirror webhook (#uts-live-alerts; same agent-orchestrator-alerts app)
_SM_UTS_LIVE_ALERTS_SLACK_WEBHOOK = "alerting-uts-live-alerts-slack-webhook"

_ALL_PAGING_SM_KEYS = (
    _SM_BOT_TOKEN,
    _SM_CHAT_ID,
    _SM_CHAT_ID_OPS,
    _SM_TWILIO_ACCOUNT_SID,
    _SM_TWILIO_AUTH_TOKEN,
    _SM_TWILIO_FROM_NUMBER,
    _SM_TWILIO_TO_PRIMARY,
    _SM_TWILIO_TO_SECONDARY,
    _SM_TWILIO_TO_FOUNDER,
    _SM_UTS_LIVE_ALERTS_SLACK_WEBHOOK,
)

_PAGING_REFRESH_INTERVAL = 300.0  # 5 minutes


class _PagingCredentialsReloader:
    """Periodic SM hot-reload for Telegram + Twilio paging credentials.

    Reads all paging-layer secrets from GCP Secret Manager every 300 s.
    Thread-safe atomic swap. Falls back to empty strings (caller should use
    env-var values from config) when SM is unavailable or project_id is
    unset (mock mode).
    """

    def __init__(self, refresh_interval: float = _PAGING_REFRESH_INTERVAL) -> None:
        self._project_id: str | None = None
        self._refresh_interval = refresh_interval
        self._credentials: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    def get_bot_token(self) -> str:
        with self._lock:
            return self._credentials.get("bot_token") or ""  # noqa: qg-empty-fallback

    def get_chat_id(self) -> str:
        with self._lock:
            return self._credentials.get("chat_id") or ""  # noqa: qg-empty-fallback

    def get_chat_id_ops(self) -> str:
        with self._lock:
            return self._credentials.get("chat_id_ops") or ""  # noqa: qg-empty-fallback

    def get_twilio_account_sid(self) -> str:
        with self._lock:
            return self._credentials.get("twilio_account_sid") or ""  # noqa: qg-empty-fallback

    def get_twilio_auth_token(self) -> str:
        with self._lock:
            return self._credentials.get("twilio_auth_token") or ""  # noqa: qg-empty-fallback

    def get_twilio_from_number(self) -> str:
        with self._lock:
            return self._credentials.get("twilio_from_number") or ""  # noqa: qg-empty-fallback

    def get_twilio_to_primary(self) -> str:
        with self._lock:
            return self._credentials.get("twilio_to_number_primary") or ""  # noqa: qg-empty-fallback

    def get_twilio_to_secondary(self) -> str:
        with self._lock:
            return self._credentials.get("twilio_to_number_secondary") or ""  # noqa: qg-empty-fallback

    def get_twilio_to_founder(self) -> str:
        with self._lock:
            return self._credentials.get("twilio_to_number_founder") or ""  # noqa: qg-empty-fallback

    def get_uts_live_alerts_slack_webhook(self) -> str:
        with self._lock:
            return self._credentials.get("uts_live_alerts_slack_webhook") or ""  # noqa: qg-empty-fallback

    def start(self, project_id: str | None) -> None:
        if self._started:
            return
        self._project_id = project_id
        initial = self._fetch()
        with self._lock:
            self._credentials = initial
        self._started = True
        if not initial or not project_id:
            logger.info("PagingCredentialsReloader: no SM credentials loaded (mock mode or no project_id)")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="paging-creds-reloader")
        self._thread.start()
        logger.info("PagingCredentialsReloader started: refresh every %ds", int(self._refresh_interval))

    def stop(self) -> None:
        self._started = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None

    def _fetch(self) -> dict[str, str]:
        if not self._project_id:
            return {}
        try:
            client = SecretManagerClient(project_id=self._project_id)
            raw = client.get_secrets(list(_ALL_PAGING_SM_KEYS))
            result: dict[str, str] = {}
            _mapping = {
                _SM_BOT_TOKEN: "bot_token",
                _SM_CHAT_ID: "chat_id",
                _SM_CHAT_ID_OPS: "chat_id_ops",
                _SM_TWILIO_ACCOUNT_SID: "twilio_account_sid",
                _SM_TWILIO_AUTH_TOKEN: "twilio_auth_token",
                _SM_TWILIO_FROM_NUMBER: "twilio_from_number",
                _SM_TWILIO_TO_PRIMARY: "twilio_to_number_primary",
                _SM_TWILIO_TO_SECONDARY: "twilio_to_number_secondary",
                _SM_TWILIO_TO_FOUNDER: "twilio_to_number_founder",
                _SM_UTS_LIVE_ALERTS_SLACK_WEBHOOK: "uts_live_alerts_slack_webhook",
            }
            for sm_key, cred_key in _mapping.items():
                val = raw.get(sm_key)
                if val:
                    result[cred_key] = val
            return result
        except Exception as exc:
            logger.warning("PagingCredentialsReloader: SM fetch failed (keeping old): %s", exc)
            return {}

    def _refresh(self) -> None:
        new_creds = self._fetch()
        if not new_creds:
            return
        with self._lock:
            old_creds = self._credentials
            self._credentials = new_creds
        changed = {k for k in set(new_creds) | set(old_creds) if new_creds.get(k) != old_creds.get(k)}
        if changed:
            logger.info("Paging credentials refreshed from SM: %s", sorted(changed))
            log_event(
                "CONFIG_CHANGED",
                details={
                    "domain": "paging-credentials",
                    "service": "alerting-service",
                    "changed": sorted(changed),
                },
            )

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._refresh_interval)
            if self._stop_event.is_set():
                break
            try:
                self._refresh()
            except Exception as exc:
                logger.warning("Paging credentials refresh failed (keeping old): %s", exc)


# Module-level singleton — shared between alert_handler + router
_paging_creds_reloader = _PagingCredentialsReloader()


def get_paging_credentials() -> dict[str, str]:
    """Return current SM-loaded paging credentials.

    Telegram keys: ``bot_token``, ``chat_id``, ``chat_id_ops``.
    Twilio keys: ``twilio_account_sid``, ``twilio_auth_token``,
    ``twilio_from_number``, ``twilio_to_number_primary``,
    ``twilio_to_number_secondary``, ``twilio_to_number_founder``.
    Slack mirror key: ``uts_live_alerts_slack_webhook``.
    Empty string for keys not yet provisioned in SM.
    """
    return {
        "bot_token": _paging_creds_reloader.get_bot_token(),
        "chat_id": _paging_creds_reloader.get_chat_id(),
        "chat_id_ops": _paging_creds_reloader.get_chat_id_ops(),
        "twilio_account_sid": _paging_creds_reloader.get_twilio_account_sid(),
        "twilio_auth_token": _paging_creds_reloader.get_twilio_auth_token(),
        "twilio_from_number": _paging_creds_reloader.get_twilio_from_number(),
        "twilio_to_number_primary": _paging_creds_reloader.get_twilio_to_primary(),
        "twilio_to_number_secondary": _paging_creds_reloader.get_twilio_to_secondary(),
        "twilio_to_number_founder": _paging_creds_reloader.get_twilio_to_founder(),
        "uts_live_alerts_slack_webhook": _paging_creds_reloader.get_uts_live_alerts_slack_webhook(),
    }


def start_paging_credentials_reloader(service_config: AlertingSystemConfig) -> None:
    """Start the SM paging-credentials hot-reload. Call on service startup."""
    _paging_creds_reloader.start(project_id=service_config.gcp_project_id)


def stop_paging_credentials_reloader() -> None:
    """Stop the SM paging-credentials hot-reload. Call on service shutdown."""
    _paging_creds_reloader.stop()
    logger.info("Paging credentials reloader stopped")


_instrument_reloader: DomainConfigReloader[InstrumentDomainConfig] | None = None
_venue_reloader: DomainConfigReloader[VenueDomainConfig] | None = None
_alert_rule_reloader: DomainConfigReloader[AlertRuleDomainConfig] | None = None

_active_instruments: InstrumentDomainConfig | None = None
_active_venues: VenueDomainConfig | None = None
_active_alert_rules: AlertRuleDomainConfig | None = None


def get_active_instruments() -> InstrumentDomainConfig | None:
    """Return the latest instruments domain config snapshot, or None if not yet loaded."""
    return _active_instruments


def get_active_venues() -> VenueDomainConfig | None:
    """Return the latest venues domain config snapshot, or None if not yet loaded."""
    return _active_venues


def get_active_alert_rules() -> AlertRuleDomainConfig | None:
    """Return the latest alert-rules domain config snapshot, or None if not yet loaded."""
    return _active_alert_rules


def _on_instruments_reload(config: InstrumentDomainConfig) -> None:
    global _active_instruments
    _active_instruments = config  # Atomic swap -- single assignment
    logger.info(
        "Instruments domain config reloaded: %d instruments, %d venues",
        len(config.subscription_list),
        len(config.enabled_venues),
    )
    log_event(
        "CONFIG_CHANGED",
        details={
            "domain": "instruments",
            "service": "alerting-service",
            "instruments_count": len(config.subscription_list),
            "venues_count": len(config.enabled_venues),
        },
    )


def _on_venues_reload(config: VenueDomainConfig) -> None:
    global _active_venues
    _active_venues = config  # Atomic swap -- single assignment
    logger.info(
        "Venues domain config reloaded: %d enabled venues",
        len(config.enabled_venues),
    )
    log_event(
        "CONFIG_CHANGED",
        details={
            "domain": "venues",
            "service": "alerting-service",
            "enabled_venues_count": len(config.enabled_venues),
        },
    )


def _on_alert_rules_reload(config: AlertRuleDomainConfig) -> None:
    global _active_alert_rules
    _active_alert_rules = config  # Atomic swap -- single assignment
    logger.info(
        "Alert rules domain config reloaded: %d rules, %d service overrides",
        len(config.rules),
        len(config.service_overrides),
    )
    log_event(
        "CONFIG_CHANGED",
        details={
            "domain": "alert-rules",
            "service": "alerting-service",
            "rules_count": len(config.rules),
            "service_overrides_count": len(config.service_overrides),
        },
    )


def start_domain_config_reloaders(service_config: AlertingSystemConfig) -> None:
    """Start domain config reloaders. Call on service startup."""
    global _instrument_reloader, _venue_reloader, _alert_rule_reloader

    config_store_bucket: str = service_config.config_store_bucket
    project_id: str | None = service_config.gcp_project_id

    if not config_store_bucket:
        logger.info("CONFIG_STORE_BUCKET not set — domain config hot-reload disabled")
        return

    _instrument_reloader = DomainConfigReloader(
        domain="instruments",
        config_class=InstrumentDomainConfig,
        config_bucket=config_store_bucket,
        project_id=project_id,
    )
    _instrument_reloader.on_reload(_on_instruments_reload)
    _instrument_reloader.start_watching()

    _venue_reloader = DomainConfigReloader(
        domain="venues",
        config_class=VenueDomainConfig,
        config_bucket=config_store_bucket,
        project_id=project_id,
    )
    _venue_reloader.on_reload(_on_venues_reload)
    _venue_reloader.start_watching()

    _alert_rule_reloader = DomainConfigReloader(
        domain="alert-rules",
        config_class=AlertRuleDomainConfig,
        config_bucket=config_store_bucket,
        project_id=project_id,
    )
    _alert_rule_reloader.on_reload(_on_alert_rules_reload)
    _alert_rule_reloader.start_watching()

    logger.info("Domain config reloaders started: instruments, venues, alert-rules")


def stop_domain_config_reloaders() -> None:
    """Stop domain config reloaders. Call on service shutdown."""
    global _instrument_reloader, _venue_reloader, _alert_rule_reloader
    if _instrument_reloader is not None:
        _instrument_reloader.stop_watching()
        _instrument_reloader = None
    if _venue_reloader is not None:
        _venue_reloader.stop_watching()
        _venue_reloader = None
    if _alert_rule_reloader is not None:
        _alert_rule_reloader.stop_watching()
        _alert_rule_reloader = None
    logger.info("Domain config reloaders stopped")
