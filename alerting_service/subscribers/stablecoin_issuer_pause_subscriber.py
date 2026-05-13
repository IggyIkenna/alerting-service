"""Stablecoin issuer-pause subscriber (D.5).

Polls three issuer signals for pause/halt conditions:
1. Circle attestations endpoint (USDC) — HTTP poll for attestation halt.
2. Tether transparency page (USDT) — HTTP poll for halt notices.
3. MakerDAO PSM contract (DAI) — on-chain query of PSM module enabled/disabled state.

Emits ``AlertCode.STABLECOIN_ISSUER_PAUSED`` when a pause is detected.

Event payload schema::

    {
        "event_name": "STABLECOIN_ISSUER_PAUSED",
        "stable": "USDC",
        "issuer": "Circle",
        "paused_at": "2026-05-13T12:00:00Z",
        "source_url": "https://www.circle.com/en/transparency/...",
        "reason": "Attestation halt detected",
        "correlation_id": "<uuid>",
    }

SSOT: ``plans/active/risk_simulations_limits_alerting_2026_05_10.md`` Phase D.5.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import httpx
from unified_api_contracts.alerting import AlertCode

logger = logging.getLogger(__name__)

# Poll interval — 60s between checks per issuer
_DEFAULT_POLL_INTERVAL_SECONDS: float = 60.0

# Circle attestation endpoint
_CIRCLE_ATTESTATION_URL: str = (
    "https://www.circle.com/en/transparency/attestations/usdc-attestation"
)

# Tether transparency URL
_TETHER_TRANSPARENCY_URL: str = "https://tether.to/en/transparency/"

# MakerDAO PSM contract address (PSM-USDC-A on Ethereum mainnet)
_MAKERDAO_PSM_ADDRESS: str = "0x89B78CfA322F6C5dE0aBcEecab66Aee45393cC5A"

# Halt signal keywords — case-insensitive substring match in HTTP response body
_CIRCLE_HALT_KEYWORDS: tuple[str, ...] = (
    "attestation halt",
    "issuance paused",
    "paused operations",
    "service interruption",
    "redemption suspended",
)
_TETHER_HALT_KEYWORDS: tuple[str, ...] = (
    "tether halted",
    "usdt paused",
    "suspension",
    "freeze active",
    "redemptions paused",
)


@dataclass
class IssuePauseEvent:
    """A detected issuer-pause event, ready to emit as an alert."""

    stable: str
    issuer: str
    paused_at: datetime
    source_url: str
    reason: str
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_alert_payload(self) -> dict[str, object]:
        """Serialize to alert event payload dict."""
        return {
            "event_name": AlertCode.STABLECOIN_ISSUER_PAUSED.value,
            "stable": self.stable,
            "issuer": self.issuer,
            "paused_at": self.paused_at.isoformat(),
            "source_url": self.source_url,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
        }


class AlertEmitter(Protocol):
    """Protocol for emitting alert payloads from subscribers."""

    def emit(self, payload: dict[str, object]) -> None:
        """Emit a single alert payload to the alerting bus."""
        ...


class CircleAttestationPoller:
    """Polls Circle attestations endpoint for USDC issuance halt signals.

    Detects a halt by scanning the response body for known halt keywords.
    In production, operators may upgrade this to parse the structured JSON
    attestation document once Circle publishes a machine-readable format.
    """

    def __init__(
        self,
        url: str = _CIRCLE_ATTESTATION_URL,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._url = url
        self._timeout = timeout_seconds

    async def check(self, client: httpx.AsyncClient) -> IssuePauseEvent | None:
        """Perform one poll; return an event if a halt is detected, else None."""
        try:
            response = await client.get(self._url, timeout=self._timeout, follow_redirects=True)
            body = response.text.lower()
        except httpx.HTTPError as exc:
            logger.warning("CircleAttestationPoller HTTP error: %s", exc)
            return None

        for keyword in _CIRCLE_HALT_KEYWORDS:
            if keyword in body:
                logger.warning(
                    "CircleAttestationPoller: halt keyword '%s' detected at %s",
                    keyword,
                    self._url,
                )
                return IssuePauseEvent(
                    stable="USDC",
                    issuer="Circle",
                    paused_at=datetime.now(UTC),
                    source_url=self._url,
                    reason=f"Attestation halt detected: keyword='{keyword}'",
                )
        return None


class TetherAttestationPoller:
    """Polls Tether transparency page for USDT halt notices."""

    def __init__(
        self,
        url: str = _TETHER_TRANSPARENCY_URL,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._url = url
        self._timeout = timeout_seconds

    async def check(self, client: httpx.AsyncClient) -> IssuePauseEvent | None:
        """Perform one poll; return an event if a halt is detected, else None."""
        try:
            response = await client.get(self._url, timeout=self._timeout, follow_redirects=True)
            body = response.text.lower()
        except httpx.HTTPError as exc:
            logger.warning("TetherAttestationPoller HTTP error: %s", exc)
            return None

        for keyword in _TETHER_HALT_KEYWORDS:
            if keyword in body:
                logger.warning(
                    "TetherAttestationPoller: halt keyword '%s' detected at %s",
                    keyword,
                    self._url,
                )
                return IssuePauseEvent(
                    stable="USDT",
                    issuer="Tether",
                    paused_at=datetime.now(UTC),
                    source_url=self._url,
                    reason=f"Halt notice detected: keyword='{keyword}'",
                )
        return None


class MakerDAOPSMPoller:
    """Queries MakerDAO PSM module enabled/disabled state.

    Uses the PSM ``live`` storage slot via eth_call JSON-RPC to check
    whether the PSM is still operational. A PSM with ``live == 0`` is
    permanently disabled (cage() was called). A ``live == 1`` indicates
    normal operation.

    In unit tests, inject a mock ``web3_call_fn`` to avoid real RPC calls.
    """

    def __init__(
        self,
        rpc_url: str | None = None,
        psm_address: str = _MAKERDAO_PSM_ADDRESS,
        web3_call_fn: object | None = None,
    ) -> None:
        """
        :param rpc_url: Ethereum JSON-RPC endpoint (ADC-resolved in production).
        :param psm_address: PSM contract address.
        :param web3_call_fn: Optional callable ``(address) -> int`` for testing.
            If provided, ``rpc_url`` is ignored.
        """
        self._rpc_url = rpc_url
        self._psm_address = psm_address
        self._web3_call_fn = web3_call_fn

    async def _read_psm_live(self) -> int | None:
        """Read the PSM ``live`` storage slot.

        Returns 1 for live, 0 for caged, None on error.
        """
        if self._web3_call_fn is not None:
            # Test injection path — call synchronous fn in executor
            loop = asyncio.get_event_loop()
            try:
                result: int = await loop.run_in_executor(
                    None,
                    self._web3_call_fn,
                    self._psm_address,  # type: ignore[arg-type]
                )
                return result
            except Exception as exc:
                logger.warning("MakerDAOPSMPoller mock call error: %s", exc)
                return None

        # Production path: minimal eth_call via httpx (avoids web3.py import coupling)
        if self._rpc_url is None:
            logger.warning("MakerDAOPSMPoller: no rpc_url configured, skipping check")
            return None

        # ``live()`` selector: keccak256("live()")[0:4] = 0x957aa58c
        call_data: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {"to": self._psm_address, "data": "0x957aa58c"},
                "latest",
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self._rpc_url, json=call_data)
                resp.raise_for_status()
                result_hex: str = resp.json().get("result", "0x1")
                return int(result_hex, 16)
        except Exception as exc:
            logger.warning("MakerDAOPSMPoller RPC call failed: %s", exc)
            return None

    async def check(self) -> IssuePauseEvent | None:
        """Return an event if the MakerDAO PSM is caged (live == 0), else None."""
        live_value = await self._read_psm_live()
        if live_value is None:
            return None  # Probe failed — don't false-positive on probe errors
        if live_value == 0:
            logger.warning(
                "MakerDAOPSMPoller: PSM %s caged (live==0) — DAI redemptions disabled",
                self._psm_address,
            )
            return IssuePauseEvent(
                stable="DAI",
                issuer="MakerDAO",
                paused_at=datetime.now(UTC),
                source_url=f"https://etherscan.io/address/{self._psm_address}",
                reason="PSM contract caged: live()==0. DAI redemptions via PSM disabled.",
            )
        return None


class StablecoinIssuerPauseSubscriber:
    """Runs all three issuer-pause pollers on a schedule.

    Wires Circle + Tether HTTP pollers + MakerDAO contract poller into a
    single async loop. Each detected pause event is emitted via the injected
    ``AlertEmitter``.

    Usage::

        from alerting_service.notifiers.router import route_event

        class _RouterEmitter:
            def emit(self, payload: dict[str, object]) -> None:
                event_name = str(payload.get("event_name", "STABLECOIN_ISSUER_PAUSED"))
                route_event(event_name, payload)

        subscriber = StablecoinIssuerPauseSubscriber(emitter=_RouterEmitter())
        await subscriber.run_until_stopped()
    """

    def __init__(
        self,
        emitter: AlertEmitter,
        circle_poller: CircleAttestationPoller | None = None,
        tether_poller: TetherAttestationPoller | None = None,
        makerdao_poller: MakerDAOPSMPoller | None = None,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._emitter = emitter
        self._circle = circle_poller or CircleAttestationPoller()
        self._tether = tether_poller or TetherAttestationPoller()
        self._makerdao = makerdao_poller or MakerDAOPSMPoller()
        self._poll_interval = poll_interval_seconds
        self._running = False

    def stop(self) -> None:
        """Signal the polling loop to stop after the current cycle."""
        self._running = False

    async def poll_once(self) -> list[IssuePauseEvent]:
        """Run one full poll cycle across all pollers; return detected events."""
        events: list[IssuePauseEvent] = []

        async with httpx.AsyncClient() as client:
            circle_event = await self._circle.check(client)
            if circle_event is not None:
                events.append(circle_event)

            tether_event = await self._tether.check(client)
            if tether_event is not None:
                events.append(tether_event)

        makerdao_event = await self._makerdao.check()
        if makerdao_event is not None:
            events.append(makerdao_event)

        return events

    async def run_until_stopped(self) -> None:
        """Poll all issuers in a loop until ``stop()`` is called."""
        self._running = True
        logger.info(
            "StablecoinIssuerPauseSubscriber starting (interval=%.0fs)",
            self._poll_interval,
        )
        while self._running:
            events = await self.poll_once()
            for event in events:
                payload = event.to_alert_payload()
                logger.warning(
                    "Stablecoin issuer pause detected: stable=%s issuer=%s reason=%s",
                    event.stable,
                    event.issuer,
                    event.reason,
                )
                self._emitter.emit(payload)

            if self._running:
                await asyncio.sleep(self._poll_interval)

        logger.info("StablecoinIssuerPauseSubscriber stopped")


__all__ = [
    "CircleAttestationPoller",
    "IssuePauseEvent",
    "MakerDAOPSMPoller",
    "StablecoinIssuerPauseSubscriber",
    "TetherAttestationPoller",
]
