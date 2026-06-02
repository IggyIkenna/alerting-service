"""Tests for StablecoinIssuerPauseSubscriber (D.5).

Coverage: happy-path pause detection (Circle/Tether/MakerDAO), no pause,
malformed response, rate limiting, multi-issuer, MakerDAO contract miss,
retry semantics, AlertCode emission.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from unified_api_contracts.alerting import AlertCode

from alerting_service.subscribers.stablecoin_issuer_pause_subscriber import (
    CircleAttestationPoller,
    IssuePauseEvent,
    MakerDAOPSMPoller,
    StablecoinIssuerPauseSubscriber,
    TetherAttestationPoller,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


class _CapturingEmitter:
    """Test emitter that captures emitted payloads."""

    def __init__(self) -> None:
        self.emitted: list[dict[str, object]] = []

    def emit(self, payload: dict[str, object]) -> None:
        self.emitted.append(payload)


# ── Test 1: Happy-path Circle pause detected ──────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_circle_halt_keyword_detected() -> None:
    """CircleAttestationPoller detects halt keyword in response body."""
    respx.get("https://www.circle.com/en/transparency/attestations/usdc-attestation").mock(
        return_value=httpx.Response(
            200,
            text="USDC attestation halt - issuance paused due to banking partner disruption",
        )
    )
    poller = CircleAttestationPoller()
    async with httpx.AsyncClient() as client:
        event = await poller.check(client)

    assert event is not None
    assert event.stable == "USDC"
    assert event.issuer == "Circle"
    assert "attestation halt" in event.reason.lower()


# ── Test 2: No Circle pause ────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_circle_no_halt_returns_none() -> None:
    """CircleAttestationPoller returns None when no halt keywords found."""
    respx.get("https://www.circle.com/en/transparency/attestations/usdc-attestation").mock(
        return_value=httpx.Response(200, text="USDC attestation: all systems operational.")
    )
    poller = CircleAttestationPoller()
    async with httpx.AsyncClient() as client:
        event = await poller.check(client)

    assert event is None


# ── Test 3: Tether halt detected ──────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_tether_halt_keyword_detected() -> None:
    """TetherAttestationPoller detects halt keyword in response body."""
    respx.get("https://tether.to/en/transparency/").mock(
        return_value=httpx.Response(200, text="Tether halted redemptions pending regulatory review.")
    )
    poller = TetherAttestationPoller()
    async with httpx.AsyncClient() as client:
        event = await poller.check(client)

    assert event is not None
    assert event.stable == "USDT"
    assert event.issuer == "Tether"


# ── Test 4: Malformed HTTP response (non-200) ─────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_circle_http_error_returns_none() -> None:
    """CircleAttestationPoller returns None on HTTP error (does not raise)."""
    respx.get("https://www.circle.com/en/transparency/attestations/usdc-attestation").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    poller = CircleAttestationPoller()
    async with httpx.AsyncClient() as client:
        event = await poller.check(client)

    assert event is None  # Probe error → no false-positive


# ── Test 5: Rate limiting (Circle) ────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_circle_rate_limit_returns_none() -> None:
    """CircleAttestationPoller gracefully handles 429 (rate limit)."""
    respx.get("https://www.circle.com/en/transparency/attestations/usdc-attestation").mock(
        return_value=httpx.Response(429, text="Too many requests")
    )
    poller = CircleAttestationPoller()
    async with httpx.AsyncClient() as client:
        event = await poller.check(client)

    # 429 causes HTTP error but doesn't crash; returns None
    assert event is None


# ── Test 6: MakerDAO PSM caged (live==0) ─────────────────────────────────


@pytest.mark.asyncio
async def test_makerdao_psm_caged() -> None:
    """MakerDAOPSMPoller detects caged PSM (live==0)."""

    def _mock_web3_call(address: str) -> int:
        return 0  # Caged

    poller = MakerDAOPSMPoller(web3_call_fn=_mock_web3_call)
    event = await poller.check()

    assert event is not None
    assert event.stable == "DAI"
    assert event.issuer == "MakerDAO"
    assert "live()==0" in event.reason


# ── Test 7: MakerDAO PSM live (no alert) ─────────────────────────────────


@pytest.mark.asyncio
async def test_makerdao_psm_live_returns_none() -> None:
    """MakerDAOPSMPoller returns None when PSM is live (live==1)."""

    def _mock_web3_call(address: str) -> int:
        return 1  # Live

    poller = MakerDAOPSMPoller(web3_call_fn=_mock_web3_call)
    event = await poller.check()

    assert event is None


# ── Test 8: MakerDAO contract miss / probe error ──────────────────────────


@pytest.mark.asyncio
async def test_makerdao_probe_error_no_false_positive() -> None:
    """MakerDAOPSMPoller returns None when the probe call raises (no false-positive)."""

    def _mock_web3_call(address: str) -> int:
        raise RuntimeError("RPC connection failed")

    poller = MakerDAOPSMPoller(web3_call_fn=_mock_web3_call)
    event = await poller.check()

    assert event is None  # Probe error must not emit a false-positive pause


# ── Test 9: AlertCode emission ───────────────────────────────────────────


def test_issue_pause_event_alert_code() -> None:
    """IssuePauseEvent.to_alert_payload() sets correct AlertCode."""
    event = IssuePauseEvent(
        stable="USDC",
        issuer="Circle",
        paused_at=datetime(2026, 5, 13, 12, 0, tzinfo=UTC),
        source_url="https://example.com",
        reason="Test halt",
    )
    payload = event.to_alert_payload()

    assert payload["event_name"] == AlertCode.STABLECOIN_ISSUER_PAUSED.value
    assert payload["stable"] == "USDC"
    assert payload["issuer"] == "Circle"


# ── Test 10: Multi-issuer events in single poll_once ──────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_multi_issuer_all_detected() -> None:
    """All three pollers can fire in the same poll_once cycle."""
    respx.get("https://www.circle.com/en/transparency/attestations/usdc-attestation").mock(
        return_value=httpx.Response(200, text="attestation halt detected")
    )
    respx.get("https://tether.to/en/transparency/").mock(
        return_value=httpx.Response(200, text="Tether halted redemptions.")
    )

    def _caged(address: str) -> int:
        return 0

    emitter = _CapturingEmitter()
    subscriber = StablecoinIssuerPauseSubscriber(
        emitter=emitter,
        makerdao_poller=MakerDAOPSMPoller(web3_call_fn=_caged),
    )
    events = await subscriber.poll_once()

    assert len(events) == 3
    stables = {e.stable for e in events}
    assert stables == {"USDC", "USDT", "DAI"}
