"""Unit tests for ``dependency_health_runner`` — the production wiring for
execution-service / strategy-service dependency-health probing.

The ``TestDeliberatelyInducedSev0ReachesKillSwitch`` class is the direct proof
for ``live_path_has_no_stale_producer_revocation_2026_08_14.md`` item 1's
"Done when" bar: a deliberately-induced SEV0 condition fires the actuator and
the kill-switch bus receives it — end-to-end through the REAL prober, REAL
handler, and REAL rule ladder, with only the bus + paging boundary mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from unified_api_contracts import KillSwitchScope

from alerting_service.config import AlertingSystemConfig
from alerting_service.dependency_health_prober import DependencyHealthProber
from alerting_service.dependency_health_runner import (
    INTERNAL_SERVICE_POLICIES,
    make_probe_fn,
)


def _execution_policy():
    return next(p for p in INTERNAL_SERVICE_POLICIES if p.dependency_id == "execution_service_health")


def _strategy_policy():
    return next(p for p in INTERNAL_SERVICE_POLICIES if p.dependency_id == "strategy_service_health")


class _Clock:
    """Controllable monotonic clock — mirrors test_dependency_health_prober.py's _FakeClock."""

    def __init__(self) -> None:
        self._now = 1000.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _mock_async_client(mock_resp: MagicMock | None = None, side_effect: Exception | None = None) -> MagicMock:
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    if side_effect is not None:
        mock_client.get = AsyncMock(side_effect=side_effect)
    else:
        mock_client.get = AsyncMock(return_value=mock_resp)
    return mock_client


class TestInternalServicePolicies:
    def test_both_services_registered_with_kill_switch_scope(self) -> None:
        by_id = {p.dependency_id: p for p in INTERNAL_SERVICE_POLICIES}
        assert by_id["execution_service_health"].kill_switch_scope == KillSwitchScope.GLOBAL
        assert by_id["strategy_service_health"].kill_switch_scope == KillSwitchScope.STRATEGY
        assert all(p.fallback_available is False for p in INTERNAL_SERVICE_POLICIES)


class TestMakeProbeFn:
    async def test_unconfigured_url_fails_open(self) -> None:
        config = AlertingSystemConfig()
        probe = make_probe_fn(config)
        assert await probe(_execution_policy()) is True

    async def test_healthy_endpoint_returns_true(self) -> None:
        config = AlertingSystemConfig(execution_service_health_url="https://execution.example/health")
        probe = make_probe_fn(config)
        mock_client = _mock_async_client(MagicMock(status_code=200))
        with patch("alerting_service.dependency_health_runner.httpx.AsyncClient", return_value=mock_client):
            assert await probe(_execution_policy()) is True

    async def test_unhealthy_status_code_returns_false(self) -> None:
        config = AlertingSystemConfig(execution_service_health_url="https://execution.example/health")
        probe = make_probe_fn(config)
        mock_client = _mock_async_client(MagicMock(status_code=503))
        with patch("alerting_service.dependency_health_runner.httpx.AsyncClient", return_value=mock_client):
            assert await probe(_execution_policy()) is False

    async def test_network_error_returns_false(self) -> None:
        config = AlertingSystemConfig(execution_service_health_url="https://execution.example/health")
        probe = make_probe_fn(config)
        mock_client = _mock_async_client(side_effect=httpx.ConnectError("refused"))
        with patch("alerting_service.dependency_health_runner.httpx.AsyncClient", return_value=mock_client):
            assert await probe(_execution_policy()) is False

    async def test_strategy_service_unconfigured_also_fails_open(self) -> None:
        config = AlertingSystemConfig()
        probe = make_probe_fn(config)
        assert await probe(_strategy_policy()) is True


class TestDeliberatelyInducedSev0ReachesKillSwitch:
    """The 'Done when' bar: a deliberately-induced SEV0 fires the actuator and
    the kill-switch bus receives it."""

    async def _drive_to_sev0(self, policy) -> MagicMock:
        clock = _Clock()

        async def _always_down(_p: object) -> bool:
            return False

        prober = DependencyHealthProber([policy], probe_fn=_always_down, consecutive_fail_threshold=3, clock=clock)
        mock_bus = MagicMock()
        with (
            patch("alerting_service.dependency_health_event_handler.route_event_with_explicit_channels"),
            patch(
                "alerting_service.dependency_health_event_handler.get_kill_switch_bus",
                return_value=mock_bus,
            ),
        ):
            # 3 consecutive failed probes opens the outage clock (threshold=3).
            for _ in range(3):
                await prober.probe_all()
                clock.advance(1.0)
            # Advance well past hard_escalation_seconds to force the SEV0 tier.
            clock.advance(policy.hard_escalation_seconds + 1)
            await prober.probe_all()
        return mock_bus

    async def test_execution_service_sev0_arms_global_kill_switch(self) -> None:
        mock_bus = await self._drive_to_sev0(_execution_policy())
        mock_bus.fire.assert_called_once()
        args, kwargs = mock_bus.fire.call_args
        assert args[0] == KillSwitchScope.GLOBAL
        assert args[1] is None  # wildcard — whole-service probe, no per-key scope
        assert kwargs["fired_by"] == "alerting-service:dependency_health"

    async def test_strategy_service_sev0_arms_strategy_kill_switch(self) -> None:
        mock_bus = await self._drive_to_sev0(_strategy_policy())
        mock_bus.fire.assert_called_once()
        args, kwargs = mock_bus.fire.call_args
        assert args[0] == KillSwitchScope.STRATEGY
        assert args[1] is None
        assert kwargs["fired_by"] == "alerting-service:dependency_health"
