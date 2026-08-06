"""Email fallback notifier — CRITICAL-severity SMTP send when PagerDuty is
unavailable/fails.

Finding (2026-08-06): ``AlertingSystemConfig`` declared ``email_smtp_host`` /
``email_smtp_port`` / ``email_to`` fields with ZERO consumers anywhere in this
repo — no notifier module, no ``smtplib``/``sendgrid``/SES usage. Checked
``unified-trading-library`` first (the only shared dep this T4 service may
import per the no-service-to-service-deps rule) for a reusable email utility —
none exists (zero ``smtplib``/``sendgrid``/SES consumers there either), so
this is implemented directly with the Python stdlib ``smtplib`` +
``email.message`` — no new third-party dependency.

Called from ``notifiers/router._deliver_to_channels`` ONLY when a CRITICAL
event's PagerDuty delivery is unavailable or fails (``pagerduty.send_event``
returns ``False``) — the last-resort channel so a CRITICAL incident is never
fully silent. Host/port are plain ``AlertingSystemConfig`` fields (non-secret);
auth username/password/from-address are SM-hot-reloaded via
``config_reloaders.get_paging_credentials()`` (mirrors the Twilio/Telegram
credential wiring in the same reloader) — NEVER hardcoded, NEVER plaintext in
code. Empty host or empty recipient list means the fallback is simply not
configured yet — logs once and returns ``False``, never raises.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from unified_trading_library import log_event

from ..config import AlertingSystemConfig
from ..config_reloaders import get_paging_credentials

logger = logging.getLogger(__name__)

_SEND_TIMEOUT_SECONDS = 10.0


def _send_smtp(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    from_address: str,
    to_addresses: list[str],
    subject: str,
    body: str,
) -> bool:
    """Send one plaintext email via SMTP STARTTLS. Never raises.

    Returns ``False`` (logged) on missing config, auth failure, or any SMTP/
    network error — the caller (``send_critical_fallback``) treats that as
    "email fallback also unavailable", never a crash.
    """
    if not smtp_host or not to_addresses:
        logger.warning(
            "Email fallback not configured (host=%r recipients=%d) — skipping send: %s",
            smtp_host,
            len(to_addresses),
            subject,
        )
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address or smtp_username or "alerting-service@localhost"
    message["To"] = ", ".join(to_addresses)
    message.set_content(body)
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=_SEND_TIMEOUT_SECONDS) as server:
            server.starttls()
            if smtp_username and smtp_password:
                server.login(smtp_username, smtp_password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Email fallback SMTP send failed: %s", exc)
        log_event("EMAIL_FALLBACK_FAILED", details={"subject": subject, "error": str(exc)})
        return False
    logger.info("Email fallback sent: subject=%r recipients=%d", subject, len(to_addresses))
    log_event("EMAIL_FALLBACK_SENT", details={"subject": subject, "recipients": len(to_addresses)})
    return True


def send_critical_fallback(
    summary: str,
    source: str,
    details: dict[str, object],
    config: AlertingSystemConfig,
) -> bool:
    """Send a CRITICAL-event email fallback using SM-hot-reloaded SMTP creds.

    Called ONLY for CRITICAL events whose PagerDuty delivery is unavailable or
    failed (see ``router._deliver_to_channels``). Host/port come from
    ``AlertingSystemConfig`` (non-secret, env-configurable); username/
    password/from-address prefer the SM-hot-reloaded value
    (``get_paging_credentials()``) and fall back to the matching config.py
    Field — the SAME precedence every other paging credential in this service
    uses (SM hot-reload first, env-backed config field fallback).
    """
    sm_creds = get_paging_credentials()
    smtp_username = sm_creds.get("email_smtp_username") or config.email_smtp_username
    smtp_password = sm_creds.get("email_smtp_password") or config.email_smtp_password
    from_address = sm_creds.get("email_from_address") or config.email_from_address

    subject = f"[CRITICAL] {summary}"
    body_lines = [summary, "", f"source: {source}"]
    for key in sorted(details):
        body_lines.append(f"{key}: {details[key]}")

    return _send_smtp(
        smtp_host=config.email_smtp_host or "",
        smtp_port=config.email_smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        from_address=from_address,
        to_addresses=list(config.email_to),
        subject=subject,
        body="\n".join(body_lines),
    )
