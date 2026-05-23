"""Twilio Voice notifier — Layer-3 independent fallback.

Survives PagerDuty API outage + phone-on-DND (if operator configures Twilio
number as a recognized contact). Account is DEDICATED to alerting fallback —
not shared with any other workspace tool.

Codex SSOT: ``codex/04-architecture/recovery-defence-in-depth-layers.md``
§ "Layer 3".
Implementation plan: ``plans/active/independent_fallback_twilio_voice_2026_05_23.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

# Suppress httpx INFO logging — Twilio auth_token must NEVER be logged.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_logger = logging.getLogger(__name__)

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


@dataclass(frozen=True)
class TwilioVoiceResult:
    """Outcome of a Twilio voice call attempt."""

    ok: bool
    call_sid: str | None
    """Twilio SID for the call (if accepted)."""
    http_status: int
    error_message: str | None


def send_twilio_voice(
    *,
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_number: str,
    message_text: str,
    timeout_seconds: float = 30.0,
) -> TwilioVoiceResult:
    """Place a voice call via Twilio REST API.

    Uses TwiML inline ``<Say voice="alice">`` for the message; no callback
    infrastructure needed. Hooks into the operator's mobile via standard
    cellular voice path — survives data outage on the phone.

    Args:
        account_sid: Twilio account SID (from Secret Manager).
        auth_token: Twilio auth token (from Secret Manager). NEVER LOG.
        from_number: Twilio-owned voice-capable phone number (E.164 format).
        to_number: Operator's mobile (E.164 format).
        message_text: Text to read via TwiML <Say>. Sanitize XML special chars.
        timeout_seconds: HTTP request timeout.

    Returns:
        TwilioVoiceResult. ``ok=True`` on Twilio HTTP 200/201.

    Defence-in-depth: this function never raises — failures are returned in
    the result. Caller is expected to propagate to incident state machine.
    """
    safe_text = _escape_xml(message_text)
    twiml = f'<Response><Say voice="alice">{safe_text}</Say></Response>'

    url = f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Calls.json"
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(
                url,
                auth=(account_sid, auth_token),  # Basic auth — token not in URL
                data={
                    "To": to_number,
                    "From": from_number,
                    "Twiml": twiml,
                },
            )
        if response.status_code in (200, 201):
            payload = response.json()
            call_sid = str(payload.get("sid", "")) or None
            return TwilioVoiceResult(
                ok=True,
                call_sid=call_sid,
                http_status=response.status_code,
                error_message=None,
            )
        return TwilioVoiceResult(
            ok=False,
            call_sid=None,
            http_status=response.status_code,
            error_message=response.text[:500],
        )
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        _logger.warning("Twilio voice call failed", exc_info=True)
        return TwilioVoiceResult(
            ok=False,
            call_sid=None,
            http_status=0,
            error_message=repr(exc)[:300],
        )


def _escape_xml(text: str) -> str:
    """Minimal XML-escape for safe TwiML <Say> embedding."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
