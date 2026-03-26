"""Domain config hot-reload wiring for alerting-service."""

from __future__ import annotations

import logging

from unified_trading_library import (
    AlertRuleDomainConfig,
    DomainConfigReloader,
    InstrumentDomainConfig,
    VenueDomainConfig,
    log_event,
)

from alerting_service.config import AlertingSystemConfig

logger = logging.getLogger(__name__)

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
