"""Physical pager notifier — Layer-4 final-mile fallback.

Abstract base + vendor-agnostic interface. Operator buys the device (Nokia
SIM-only phone + GSM siren combo recommended per
``codex/05-infrastructure/physical-pager-layer.md``); engineering wires the
device's webhook / SMS-trigger API behind the ``PhysicalPagerNotifier``
contract via Secret Manager creds.

Triggered ONLY by the 5 closed-set conditions in
``codex/05-infrastructure/physical-pager-layer.md`` § "Trigger conditions"
(SEV0 unacked after primary+secondary / primary provider down during SEV0
/ liquidation risk + no ack / kill switch failed + no ack / reconciliation
breach beyond hard threshold).

Codex SSOT: ``codex/04-architecture/recovery-defence-in-depth-layers.md``
§ "Layer 4".
Implementation plan: ``plans/active/physical_pager_research_and_webhook_prototype_2026_05_23.md``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

import httpx

from unified_api_contracts.alerting import AlertSeverity

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PagerNotifierResult:
    """Outcome of a physical pager notification attempt."""

    ok: bool
    vendor_message_id: str | None
    http_status: int
    error_message: str | None


class PhysicalPagerNotifier(ABC):
    """Abstract base for physical pager / siren / sat-messenger notifications.

    Subclasses implement ``send()`` for the vendor's API shape. Selection of
    which subclass to instantiate is driven by the SM secret
    ``alerting-physical-pager-vendor-name`` (closed set of strings registered
    here).
    """

    VENDOR_NAME: ClassVar[str]
    """Set by subclass. Must match the SM ``alerting-physical-pager-vendor-name`` value."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        auth_header_value: str | None = None,
        to_number: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not hasattr(self.__class__, "VENDOR_NAME"):
            raise NotImplementedError("Subclass must set VENDOR_NAME")
        self._endpoint = endpoint_url
        self._auth = auth_header_value
        self._to_number = to_number
        self._timeout = timeout_seconds

    @abstractmethod
    def send(
        self,
        *,
        severity: AlertSeverity,
        message_text: str,
        context: dict[str, str] | None = None,
    ) -> PagerNotifierResult:
        """Send a notification. Defence-in-depth: MUST NOT raise — return
        ``PagerNotifierResult(ok=False, ...)`` on failure.
        """


class PhysicalPagerNotifierWebhook(PhysicalPagerNotifier):
    """Generic webhook-based pager (LoRa gateway, Spok service, custom router).

    Posts JSON ``{"severity": "...", "message": "...", "context": {...}}`` to
    the configured endpoint with optional ``Authorization`` header.
    """

    VENDOR_NAME: ClassVar[str] = "WEBHOOK"

    def send(
        self,
        *,
        severity: AlertSeverity,
        message_text: str,
        context: dict[str, str] | None = None,
    ) -> PagerNotifierResult:
        headers = {"Content-Type": "application/json"}
        if self._auth:
            headers["Authorization"] = self._auth
        body = {
            "severity": severity.value,
            "message": message_text,
            "context": context or {},
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(self._endpoint, headers=headers, json=body)
            if 200 <= resp.status_code < 300:
                try:
                    payload = resp.json()
                    message_id = str(payload.get("id") or payload.get("message_id") or "") or None
                except Exception:  # noqa: BLE001
                    message_id = None
                return PagerNotifierResult(
                    ok=True,
                    vendor_message_id=message_id,
                    http_status=resp.status_code,
                    error_message=None,
                )
            return PagerNotifierResult(
                ok=False,
                vendor_message_id=None,
                http_status=resp.status_code,
                error_message=resp.text[:500],
            )
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            _logger.warning("PhysicalPagerNotifierWebhook failed", exc_info=True)
            return PagerNotifierResult(
                ok=False,
                vendor_message_id=None,
                http_status=0,
                error_message=repr(exc)[:300],
            )


class PhysicalPagerNotifierGsmSiren(PhysicalPagerNotifier):
    """GSM SMS-triggered siren box (Eshion / DAYTECH M5 / similar).

    Sends an SMS to the siren's SIM number; box triggers audible alarm on
    arrival. Underlying SMS transport is Twilio (defence-in-depth: even if
    PagerDuty is down, Twilio SMS path is independent).
    """

    VENDOR_NAME: ClassVar[str] = "GSM_SIREN"

    def __init__(
        self,
        *,
        endpoint_url: str,
        auth_header_value: str | None = None,
        to_number: str | None = None,
        timeout_seconds: float = 30.0,
        twilio_account_sid: str | None = None,
        twilio_auth_token: str | None = None,
        twilio_from_number: str | None = None,
    ) -> None:
        super().__init__(
            endpoint_url=endpoint_url,
            auth_header_value=auth_header_value,
            to_number=to_number,
            timeout_seconds=timeout_seconds,
        )
        self._twilio_sid = twilio_account_sid
        self._twilio_token = twilio_auth_token
        self._twilio_from = twilio_from_number

    def send(
        self,
        *,
        severity: AlertSeverity,
        message_text: str,
        context: dict[str, str] | None = None,
    ) -> PagerNotifierResult:
        if not (self._twilio_sid and self._twilio_token and self._twilio_from and self._to_number):
            return PagerNotifierResult(
                ok=False,
                vendor_message_id=None,
                http_status=0,
                error_message="GSM siren notifier not fully configured",
            )
        # Use Twilio SMS to trigger siren (siren's SIM number is to_number).
        from alerting_service.notifiers.twilio_sms import send_twilio_sms

        sms_result = send_twilio_sms(
            account_sid=self._twilio_sid,
            auth_token=self._twilio_token,
            from_number=self._twilio_from,
            to_number=self._to_number,
            message_text=f"[{severity.value}] {message_text}"[:160],
        )
        return PagerNotifierResult(
            ok=sms_result.ok,
            vendor_message_id=sms_result.message_sid,
            http_status=sms_result.http_status,
            error_message=sms_result.error_message,
        )


# Registry mapping VENDOR_NAME → subclass for SM-driven instantiation.
_REGISTRY: dict[str, type[PhysicalPagerNotifier]] = {
    PhysicalPagerNotifierWebhook.VENDOR_NAME: PhysicalPagerNotifierWebhook,
    PhysicalPagerNotifierGsmSiren.VENDOR_NAME: PhysicalPagerNotifierGsmSiren,
}


def get_physical_pager_class(vendor_name: str) -> type[PhysicalPagerNotifier]:
    """Closed-set lookup. Raises KeyError on unknown vendor."""
    if vendor_name not in _REGISTRY:
        raise KeyError(
            f"Unknown physical-pager vendor {vendor_name!r}; "
            f"registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[vendor_name]
