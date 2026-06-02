"""Primary-provider health probe — runs every 60s; emits ALERTING_PROVIDER_DEGRADED
when probe fails; flips the router into fallback-mode so SEV0 routes through
Twilio voice directly.

Codex SSOT: ``codex/04-architecture/recovery-defence-in-depth-layers.md``
§ "Layer 3" + ``codex/03-observability/alerting.md`` § "Alert provider health".

Implementation plan: ``plans/active/independent_fallback_twilio_voice_2026_05_23.md``
Phase 4.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:  # CORRECT-LOCAL: internal probe result type
    """One probe tick's outcome."""

    timestamp: datetime
    pagerduty_ok: bool
    telegram_ok: bool
    pagerduty_latency_ms: float | None = None
    telegram_latency_ms: float | None = None
    pagerduty_error: str | None = None
    telegram_error: str | None = None

    @property
    def all_ok(self) -> bool:
        """True iff BOTH primary channels healthy."""
        return self.pagerduty_ok and self.telegram_ok


@dataclass
class ProbeConfig:  # CORRECT-LOCAL: internal probe config type
    pagerduty_service_id: str | None  # None = skip PagerDuty probe
    pagerduty_api_token: str | None
    telegram_bot_token: str | None  # None = skip Telegram probe
    timeout_seconds: float = 5.0


FallbackModeChangedCallback = Callable[[bool, ProbeResult], None]
"""Caller hook fired when fallback_mode flips. Signature:
``(new_fallback_mode: bool, last_probe: ProbeResult) -> None``."""


class ProviderHealthProbe:
    """60-second cadence probe of the primary alerting providers.

    Healthy means: PagerDuty service exists + Telegram bot responds. Failure
    of either flips ``fallback_mode=True`` and the router skips PagerDuty
    on the next SEV0+ alert (routes through Twilio voice + Telegram instead).

    Per Tier-2 scaffold the probe runs in a background asyncio task spawned
    at service startup; production deployment runs as a singleton via the
    alerting-service `__main__.py`.
    """

    def __init__(
        self,
        config: ProbeConfig,
        on_fallback_change: FallbackModeChangedCallback | None = None,
    ) -> None:
        self._cfg = config
        self._on_change = on_fallback_change
        self._fallback_mode = False
        self._last_probe: ProbeResult | None = None
        self._consecutive_fails = 0
        # 2 consecutive fails → fallback_mode=True; 3 consecutive successes → reset
        self._fail_threshold = 2
        self._recover_threshold = 3
        self._consecutive_recovers = 0

    @property
    def fallback_mode(self) -> bool:
        return self._fallback_mode

    @property
    def last_probe(self) -> ProbeResult | None:
        return self._last_probe

    async def probe_once(self) -> ProbeResult:
        """Run one probe round + update fallback_mode state."""
        async with httpx.AsyncClient(timeout=self._cfg.timeout_seconds) as client:
            pd_task = asyncio.create_task(self._probe_pagerduty(client))
            tg_task = asyncio.create_task(self._probe_telegram(client))
            pd = await pd_task
            tg = await tg_task

        result = ProbeResult(
            timestamp=datetime.now(UTC),
            pagerduty_ok=pd[0],
            pagerduty_latency_ms=pd[1],
            pagerduty_error=pd[2],
            telegram_ok=tg[0],
            telegram_latency_ms=tg[1],
            telegram_error=tg[2],
        )
        self._update_fallback_mode(result)
        self._last_probe = result
        return result

    async def run_forever(self, interval_seconds: int = 60) -> None:
        """Background loop. Cancel via task.cancel() at shutdown."""
        while True:
            try:
                await self.probe_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.warning("Provider health probe iteration failed", exc_info=True)
            await asyncio.sleep(interval_seconds)

    def _update_fallback_mode(self, result: ProbeResult) -> None:
        was = self._fallback_mode
        if result.all_ok:
            self._consecutive_fails = 0
            self._consecutive_recovers += 1
            if self._fallback_mode and self._consecutive_recovers >= self._recover_threshold:
                self._fallback_mode = False
        else:
            self._consecutive_recovers = 0
            self._consecutive_fails += 1
            if not self._fallback_mode and self._consecutive_fails >= self._fail_threshold:
                self._fallback_mode = True
        if was != self._fallback_mode and self._on_change is not None:
            try:
                self._on_change(self._fallback_mode, result)
            except Exception:
                _logger.warning("on_fallback_change callback failed", exc_info=True)
            _logger.warning(
                "Alerting provider fallback_mode: %s → %s (probe=%s)",
                was,
                self._fallback_mode,
                result,
            )

    async def _probe_pagerduty(self, client: httpx.AsyncClient) -> tuple[bool, float | None, str | None]:
        if not self._cfg.pagerduty_service_id or not self._cfg.pagerduty_api_token:
            return True, None, None  # no PagerDuty configured → not a failure
        url = f"https://api.pagerduty.com/services/{self._cfg.pagerduty_service_id}"
        headers = {
            "Authorization": f"Token token={self._cfg.pagerduty_api_token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
        }
        t0 = time.monotonic()
        try:
            resp = await client.get(url, headers=headers)
            latency = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                return True, latency, None
            return False, latency, f"http {resp.status_code}"
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            return False, (time.monotonic() - t0) * 1000, repr(exc)[:200]

    async def _probe_telegram(self, client: httpx.AsyncClient) -> tuple[bool, float | None, str | None]:
        if not self._cfg.telegram_bot_token:
            return True, None, None  # no Telegram configured → not a failure
        url = f"https://api.telegram.org/bot{self._cfg.telegram_bot_token}/getMe"
        t0 = time.monotonic()
        try:
            resp = await client.get(url)
            latency = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok") and data.get("result", {}).get("is_bot"):  # noqa: qg-empty-fallback
                    return True, latency, None
                return False, latency, "bot payload invalid"
            return False, latency, f"http {resp.status_code}"
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            return False, (time.monotonic() - t0) * 1000, repr(exc)[:200]
