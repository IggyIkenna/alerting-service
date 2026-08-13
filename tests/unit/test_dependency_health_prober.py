"""Unit tests for ``dependency_health_prober``.

Verifies the producer half of the dependency-health path: per-dependency
probe state → N-consecutive-failure gate → outage clock → handler call. Probe
behaviour is injected (``probe_fn``) and time is faked (``clock``), so no
network or wall-clock is exercised.
"""

from __future__ import annotations

from unittest.mock import patch

from unified_api_contracts.dependency import DependencyClass, DependencyHealthPolicy

from alerting_service.dependency_health_prober import (
    DependencyHealthProber,
    load_dependency_policies_from_yaml,
)


def _policy(
    *,
    dependency_id: str = "binance_rest",
    test_method: str = "synthetic_probe",
) -> DependencyHealthPolicy:
    return DependencyHealthPolicy(
        dependency_id=dependency_id,
        dependency_class=DependencyClass.EXECUTION_CRITICAL_EXTERNAL,
        expected_recovery_time_seconds=60,
        warning_buffer_seconds=60,
        human_investigation_buffer_seconds=900,
        hard_escalation_seconds=1800,
        fallback_available=True,
        owner="platform",
        runbook_doc="codex/15-runbooks/incidents/rb_conn_001.md",
        test_method=test_method,
    )


class _FakeClock:
    """Controllable monotonic clock for deterministic outage-duration tests."""

    def __init__(self) -> None:
        self._now = 1000.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _ProbeScript:
    """Sequential probe answers: pop one result per probe_all() tick."""

    def __init__(self, results: list[bool]) -> None:
        self._results = list(results)
        self._index = 0

    async def __call__(self, _policy: DependencyHealthPolicy) -> bool:
        result = self._results[min(self._index, len(self._results) - 1)]
        self._index += 1
        return result


class TestConsecutiveFailureGate:
    async def test_below_threshold_does_not_open_outage(self) -> None:
        clock = _FakeClock()
        probe = DependencyHealthProber(
            [_policy()],
            probe_fn=_ProbeScript([False, False]).__call__,
            consecutive_fail_threshold=3,
            clock=clock,
        )
        with patch("alerting_service.dependency_health_prober.handle_dependency_health_payload") as mock_health:
            results = await probe.probe_all()
        clock.advance(1.0)
        await probe.probe_all()
        mock_health.assert_not_called()
        assert results[0]["reachable"] is False
        assert results[0].get("below_threshold") is True

    async def test_threshold_opens_outage_and_calls_handler(self) -> None:
        clock = _FakeClock()
        probe = DependencyHealthProber(
            [_policy()],
            probe_fn=_ProbeScript([False, False, False]).__call__,
            consecutive_fail_threshold=3,
            clock=clock,
        )
        with patch("alerting_service.dependency_health_prober.handle_dependency_health_payload") as mock_health:
            for _ in range(3):
                clock.advance(1.0)
                await probe.probe_all()
        mock_health.assert_called_once()
        payload = mock_health.call_args[0][0]
        assert payload["dependency_id"] == "binance_rest"
        # Outage clock opened at the 3rd failure (t=1003), payload computed at t=1003.
        assert float(str(payload["current_outage_seconds"])) == 0.0

    async def test_recovery_after_outage_calls_recovered_handler(self) -> None:
        clock = _FakeClock()
        probe = DependencyHealthProber(
            [_policy()],
            probe_fn=_ProbeScript([False, False, False, True]).__call__,
            consecutive_fail_threshold=3,
            clock=clock,
        )
        with (
            patch("alerting_service.dependency_health_prober.handle_dependency_health_payload"),
            patch("alerting_service.dependency_health_prober.handle_dependency_recovered_payload") as mock_recovered,
        ):
            for _ in range(3):
                clock.advance(1.0)
                await probe.probe_all()
            # The outage has lasted 5s (t=1008) when the healthy probe lands.
            clock.advance(5.0)
            results = await probe.probe_all()
        mock_recovered.assert_called_once()
        payload = mock_recovered.call_args[0][0]
        assert payload["dependency_id"] == "binance_rest"
        assert float(str(payload["previous_outage_seconds"])) == 5.0
        assert results[0].get("recovered") is True


class TestLoadPolicies:
    def test_load_policies_roundtrip(self, tmp_path) -> None:
        yaml_path = tmp_path / "policies.yaml"
        yaml_path.write_text(
            "\n".join(
                [
                    "dependencies:",
                    "  - dependency_id: binance_rest",
                    "    dependency_class: EXECUTION_CRITICAL_EXTERNAL",
                    "    expected_recovery_time_seconds: 300",
                    "    warning_buffer_seconds: 60",
                    "    human_investigation_buffer_seconds: 900",
                    "    hard_escalation_seconds: 3600",
                    "    fallback_available: true",
                    "    owner: ikenna@odum-research.com",
                    "    runbook_doc: codex/15-runbooks/incidents/RB-CONN-001.md",
                    "    test_method: synthetic_probe",
                    "",
                ]
            )
        )
        policies = load_dependency_policies_from_yaml(yaml_path)
        assert len(policies) == 1
        assert policies[0].dependency_id == "binance_rest"
        assert policies[0].test_method == "synthetic_probe"
