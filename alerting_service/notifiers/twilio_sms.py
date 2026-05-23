"""Twilio SMS notifier — cheaper Layer-3 fallback for HIGH-severity alerts.

Use when DND-bypass is not required (voice is for SEV0 wake-up). SMS works
even when push notifications are delayed and operator data is degraded.

Codex SSOT: ``codex/04-architecture/recovery-defence-in-depth-layers.md``
§ "Layer 3".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_logger = logging.getLogger(__name__)

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


@dataclass(frozen=True)
class TwilioSmsResult:
    """Outcome of a Twilio SMS send attempt."""

    ok: bool
    message_sid: str | None
    http_status: int
    error_message: str | None


def send_twilio_sms(
    *,
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_number: str,
    message_text: str,
    timeout_seconds: float = 30.0,
) -> TwilioSmsResult:
    """Send an SMS via Twilio REST API.

    Args:
        account_sid: Twilio account SID (from Secret Manager).
        auth_token: Twilio auth token (from Secret Manager). NEVER LOG.
        from_number: Twilio-owned SMS-capable number (E.164 format).
        to_number: Operator mobile (E.164 format).
        message_text: SMS body (auto-truncated to 1600 chars per Twilio limit).
        timeout_seconds: HTTP timeout.

    Returns:
        TwilioSmsResult. Defence-in-depth: never raises.
    """
    # Twilio SMS body max is 1600 chars (10 concatenated segments).
    body = message_text[:1600]

    url = f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Messages.json"
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(
                url,
                auth=(account_sid, auth_token),
                data={
                    "To": to_number,
                    "From": from_number,
                    "Body": body,
                },
            )
        if response.status_code in (200, 201):
            payload = response.json()
            message_sid = str(payload.get("sid", "")) or None
            return TwilioSmsResult(
                ok=True,
                message_sid=message_sid,
                http_status=response.status_code,
                error_message=None,
            )
        return TwilioSmsResult(
            ok=False,
            message_sid=None,
            http_status=response.status_code,
            error_message=response.text[:500],
        )
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        _logger.warning("Twilio SMS failed", exc_info=True)
        return TwilioSmsResult(
            ok=False,
            message_sid=None,
            http_status=0,
            error_message=repr(exc)[:300],
        )
