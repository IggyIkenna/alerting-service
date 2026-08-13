"""Integration test: the dependency-health path is wired end-to-end.

Proves the producer → handler → rule → router chain is actually connected: a
simulated sustained outage driven from ``DependencyHealthProber.probe_all()``
(the producer's entry point) must produce a routed CRITICAL alert, and a later
healthy probe must produce a routed DEPENDENCY_RECOVERED — with NO intermediate
step mocked. Only the router boundary (``route_event_with_explicit_channels``)
is patched, so everything between the producer and the router runs for real.

A unit test of ``evaluate_dependency_health`` would pass unchanged with this
whole path unwired (the original defect this issue was filed for) — that is the
difference this test enforces. It fails unless the real prober, handler, rule
ladder, and router are all reached.

Issue: ``/plans/active/issues/dependency_health_alerting_never_wired_2026_08_12.md``
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from unified_api_contracts.dependency import DependencyClass, DependencyHealthPolicy

from alerting_service.dependency_health_prober import DependencyHealthProber

pytestmark = pytest.mark.integration

_HARD_ESCALATION_SECONDS = 1800


def _policy(
    *,
    dependency_id: str = "binance_rest",
    fallback_available: bool = True,
) -> DependencyHealthPolicy:
    """A policy whose CRITICAL tier is reached only via ``hard_escalation_seconds``.

    ``fallback_available=True`` keeps the no-fallback short-circuit out of play
    so the CRITICAL route is exercised through the pure duration ladder — the
    no-fallback duration floor is already covered by the unit tests.
    """
    return DependencyHealthPolicy(
        dependency_id=dependency_id,
        dependency_class=DependencyClass.EXECUTION_CRITICAL_EXTERNAL,
        expected_recovery_time_seconds=60,
        warning_buffer_seconds=60,
        human_investigation_buffer_seconds=900,
        hard_escalation_seconds=_HARD_ESCALATION_SECONDS,
        fallback_available=fallback_available,
        owner="platform",
        runbook_doc="codex/15-runbooks/incidents/rb_conn_001.md",
        test_method="synthetic_probe",
    )


class _FakeClock:
    """Controllable monotonic clock for deterministic outage durations."""

    def __init__(self) -> None:
        self._now = 1000.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _ProbeScript:
    """Sequential probe answers: one result per ``probe_all()`` tick, clamped to the last."""

    def __init__(self, results: list[bool]) -> None:
        self._results = list(results)
        self._index = 0

    async def __call__(self, _policy: DependencyHealthPolicy) -> bool:
        result = self._results[min(self._index, len(self._results) - 1)]
        self._index += 1
        return result


def _router_patch_target() -> str:
    """The name the handler imported the router under — the patch surface for its call."""
    return "alerting_service.dependency_health_event_handler.route_event_with_explicit_channels"


class TestDependencyHealthWiring:
    """The producer must drive a real routed alert, not just call a mocked handler."""

    async def test_sustained_outage_routes_critical_alert(self) -> None:
        """A real outage driven from ``probe_all()`` must reach the router as CRITICAL.

        The prober's real handler + rule ladder + router all run unmocked; only
        the router boundary is patched. If any link in that chain were removed,
        this test would fail — the exact regression the unit tests cannot catch.
        """
        clock = _FakeClock()
        prober = DependencyHealthProber(
            [_policy()],
            probe_fn=_ProbeScript([False]).__call__,
            consecutive_fail_threshold=3,
            clock=clock,
        )
        with patch(_router_patch_target()) as mock_route:
            # First three failed probes open the outage clock (N-consecutive gate).
            for _ in range(3):
                clock.advance(1.0)
                await prober.probe_all()
            # Outage now exceeds the hard-escalation ceiling on the next failed probe.
            clock.advance(_HARD_ESCALATION_SECONDS)
            await prober.probe_all()

        mock_route.assert_called_once()
        args, kwargs = mock_route.call_args
        assert args[0] == "DEPENDENCY_DEGRADED_CRITICAL"
        assert kwargs["channels"] == {"pagerduty", "telegram"}
        assert kwargs["pd_severity"] == "critical"

    async def test_recovery_routes_recovered_alert(self) -> None:
        """A healthy probe after a real outage must route DEPENDENCY_RECOVERED."""
        clock = _FakeClock()
        prober = DependencyHealthProber(
            [_policy()],
            probe_fn=_ProbeScript([False, False, False, True]).__call__,
            consecutive_fail_threshold=3,
            clock=clock,
        )
        with patch(_router_patch_target()) as mock_route:
            for _ in range(3):
                clock.advance(1.0)
                await prober.probe_all()
            # A healthy probe lands after the outage has accumulated real duration.
            clock.advance(_HARD_ESCALATION_SECONDS)
            await prober.probe_all()

        mock_route.assert_called_once()
        args, kwargs = mock_route.call_args
        assert args[0] == "DEPENDENCY_RECOVERED"
        assert kwargs["channels"] == {"telegram"}
        assert kwargs["pd_severity"] is None
