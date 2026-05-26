"""Layer-3/4 fallback cascade — Twilio voice + physical pager.

Split out of ``notifiers/router.py`` so the event-router stays focused on
config-driven channel routing. This module owns the SEV0 / provider-down
fallback path: when PagerDuty is degraded or an ImmediateSev0Override fires,
the incident is dispatched to Twilio voice (Layer-3) and then the physical
pager (Layer-4).

Wired by the gateway state machine / ProviderHealthProbe ``on_fallback_change``
callback. Defence-in-depth: the entry point NEVER raises.

Codex SSOT: ``codex/04-architecture/recovery-defence-in-depth-layers.md``
§ "Layer 3 / Layer 4".
"""

from __future__ import annotations

import logging
from functools import lru_cache

from unified_api_contracts import AlertSeverity

from alerting_service.config import AlertingSystemConfig
from alerting_service.config_reloaders import get_paging_credentials

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_cloud_config() -> AlertingSystemConfig:
    """Return singleton AlertingSystemConfig instance."""
    return AlertingSystemConfig()


# envelope is typed as ``object`` (IncidentEnvelope) to avoid a hard UAC import
# at module level — the cascade reads it via getattr defensively.
def route_incident_envelope_to_fallbacks(
    envelope: object,
    *,
    immediate_sev0_overrides: tuple[str, ...] = (),
    provider_in_fallback_mode: bool = False,
) -> dict[str, bool]:
    """Dispatch IncidentEnvelope through Layer-3 (Twilio) + Layer-4 (physical
    pager) fallback channels.

    Fires when ANY of:
    - ``envelope.severity_hint == AlertSeverity.CRITICAL`` (always Twilio voice)
    - ``immediate_sev0_overrides`` non-empty (always Twilio voice + physical pager)
    - ``provider_in_fallback_mode=True`` AND severity >= HIGH (PagerDuty down; Twilio takes over)

    Returns:
        Dict ``{"twilio_voice": ok, "twilio_sms": ok, "physical_pager": ok}``
        with True = sent, False = not sent (either skipped or failed).

    Defence-in-depth: this function NEVER raises. Failures log warnings + are
    reflected in the result dict.
    """
    config = _get_cloud_config()
    sm_creds = get_paging_credentials()

    # Severity threshold for Twilio voice: CRITICAL always; HIGH when in fallback_mode.
    severity = getattr(envelope, "severity_hint", None)
    should_fire_twilio_voice = (
        bool(immediate_sev0_overrides)
        or severity == AlertSeverity.CRITICAL
        or (provider_in_fallback_mode and severity == AlertSeverity.HIGH)
    )
    should_fire_pager = bool(immediate_sev0_overrides) or severity == AlertSeverity.CRITICAL

    result = {"twilio_voice": False, "twilio_sms": False, "physical_pager": False}

    # ── Layer-3 Twilio voice ──────────────────────────────────────────────
    if should_fire_twilio_voice:
        from alerting_service.notifiers.twilio_voice import send_twilio_voice

        # SM creds take precedence over env-var config (mirror of telegram pattern)
        twilio_sid = sm_creds.get("twilio_account_sid") or config.twilio_account_sid
        twilio_token = sm_creds.get("twilio_auth_token") or config.twilio_auth_token
        twilio_from = sm_creds.get("twilio_from_number") or config.twilio_from_number
        twilio_to = sm_creds.get("twilio_to_number_primary") or config.twilio_to_number_primary

        if twilio_sid and twilio_token and twilio_from and twilio_to:
            message = (
                f"{getattr(envelope, 'severity_hint', 'ALERT')}: "
                f"{getattr(envelope, 'problem_summary', 'unknown')}. "
                f"Incident {getattr(envelope, 'incident_key', 'unknown')}. "
                f"Service {getattr(envelope, 'service', 'unknown')}."
            )
            tw_result = send_twilio_voice(
                account_sid=twilio_sid,
                auth_token=twilio_token,
                from_number=twilio_from,
                to_number=twilio_to,
                message_text=message[:300],  # TwiML <Say> length limit
            )
            result["twilio_voice"] = tw_result.ok
            if not tw_result.ok:
                logger.warning(
                    "route_incident_envelope_to_fallbacks: Twilio voice FAILED for incident=%s status=%d err=%s",
                    getattr(envelope, "incident_key", "?"),
                    tw_result.http_status,
                    (tw_result.error_message or "")[:200],
                )
        else:
            # Not configured — operator must push SM creds per ping doc item #1
            logger.warning(
                "route_incident_envelope_to_fallbacks: Twilio not configured "
                "(SM creds missing); skipping voice for incident=%s",
                getattr(envelope, "incident_key", "?"),
            )

    # ── Layer-4 physical pager ────────────────────────────────────────────
    if should_fire_pager and config.physical_pager_vendor_name:
        result["physical_pager"] = _dispatch_physical_pager(envelope, severity, config, sm_creds)

    return result


def _dispatch_physical_pager(
    envelope: object,
    severity: object,
    config: AlertingSystemConfig,
    sm_creds: dict[str, str],
) -> bool:
    """Layer-4 physical-pager leg of the fallback cascade. Never raises."""
    from alerting_service.notifiers.physical_pager import (
        PhysicalPagerNotifierGsmSiren,
        get_physical_pager_class,
    )

    try:
        pager_cls = get_physical_pager_class(config.physical_pager_vendor_name)
        if pager_cls is PhysicalPagerNotifierGsmSiren:
            notifier: object = PhysicalPagerNotifierGsmSiren(
                endpoint_url=config.physical_pager_endpoint_url,
                auth_header_value=config.physical_pager_auth_header or None,
                to_number=config.physical_pager_to_number,
                twilio_account_sid=(sm_creds.get("twilio_account_sid") or config.twilio_account_sid),
                twilio_auth_token=(sm_creds.get("twilio_auth_token") or config.twilio_auth_token),
                twilio_from_number=(sm_creds.get("twilio_from_number") or config.twilio_from_number),
            )
        else:
            notifier = pager_cls(
                endpoint_url=config.physical_pager_endpoint_url,
                auth_header_value=config.physical_pager_auth_header or None,
                to_number=config.physical_pager_to_number or None,
            )
        sev = severity if isinstance(severity, AlertSeverity) else AlertSeverity.CRITICAL
        pager_result = notifier.send(
            severity=sev,
            message_text=getattr(envelope, "problem_summary", "unknown"),
            context={
                "incident_key": getattr(envelope, "incident_key", "?"),
                "service": getattr(envelope, "service", "?"),
            },
        )
        if not pager_result.ok:
            logger.warning(
                "route_incident_envelope_to_fallbacks: physical_pager FAILED for incident=%s status=%d err=%s",
                getattr(envelope, "incident_key", "?"),
                pager_result.http_status,
                (pager_result.error_message or "")[:200],
            )
        return pager_result.ok
    except KeyError as exc:
        logger.warning(
            "route_incident_envelope_to_fallbacks: unknown physical_pager vendor %s: %s",
            config.physical_pager_vendor_name,
            exc,
        )
        return False
    except Exception:
        # defence-in-depth: the pager leg never propagates.
        logger.warning(
            "route_incident_envelope_to_fallbacks: physical_pager dispatch raised for incident=%s",
            getattr(envelope, "incident_key", "?"),
            exc_info=True,
        )
        return False
