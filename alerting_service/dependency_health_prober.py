"""Probe-driven producer for dependency-health alerts.

The missing half of ``DEPENDENCY_DEGRADED``: ``evaluate_dependency_health()`` was
defined, unit-tested and documented but called by NOTHING (issue
``dependency_health_alerting_never_wired_2026_08_12``). This module is the
producer that closes the loop — it probes each dependency on its policy's
``test_method``, tracks per-dependency last-healthy state so
``current_outage_seconds`` is DERIVED rather than reported, and feeds the result
to ``dependency_health_event_handler``.

Model:
  - ``unified_trading_library/treasury/custody_pinger.py`` — one ``_dispatch``
    on a per-dependency discriminator (``test_method``) → per-method probe.
  - ``gateway/provider_health_probe.py`` — background async probe loop with
    consecutive-failure tracking.

N-consecutive-failure gate (the producer contract documented in
``rules/connectivity_rules.py``): the outage clock starts only AFTER the Nth
consecutive failed probe, so a single flaky probe can never report a 1-second
outage. The rule enforces the DURATION floor (no-fallback SEV0 only at
``outage >= expected_recovery_time_seconds``); the consecutive-failure count is
per-dependency state kept here, not expressible in the rule.

Probe targets: the policies carry ``test_method`` but no endpoint/host/url (the
``dependency_health_policies.yaml`` entries are timing + fallback metadata only).
The built-in per-method probes are therefore SCAFFOLDS that report healthy by
default — fail-open, so a missing probe target must never page. A real probe
implementation is injected via ``probe_fn`` (tests, or a future per-method
probe wired to actual cloud/venue endpoints).

Issue: ``/plans/active/issues/dependency_health_alerting_never_wired_2026_08_12.md``
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml
from unified_api_contracts.dependency import DependencyHealthPolicy

from .dependency_health_event_handler import (
    handle_dependency_health_payload,
    handle_dependency_recovered_payload,
    set_dependency_policies,
)

logger = logging.getLogger(__name__)

#: A probe callable — returns True iff the dependency is reachable this tick.
ProbeFn = Callable[[DependencyHealthPolicy], Awaitable[bool]]

_DEFAULT_CONSECUTIVE_FAIL_THRESHOLD = 3


def load_dependency_policies_from_yaml(config_path: str | Path) -> list[DependencyHealthPolicy]:
    """Parse + validate ``dependency_health_policies.yaml`` into policies.

    Mirrors ``deployment-service/scripts/load_dependency_policies.py`` (the
    config SSOT lives in deployment-service; this helper lets alerting-service
    load the same file once a path is supplied at runtime). Fails loud on any
    invalid entry — a misconfigured policy must not be silently skipped.
    """
    raw = yaml.safe_load(Path(config_path).read_text())
    entries: list[dict[str, object]] = raw.get("dependencies", []) if isinstance(raw, dict) else []
    if not entries:
        raise ValueError(f"No 'dependencies' list found in {config_path}")
    return [DependencyHealthPolicy(**entry) for entry in entries]


class DependencyHealthProber:
    """Probes each dependency and drives outage-state transitions into the handler.

    State per ``dependency_id``:
      - ``last_healthy``: monotonic timestamp of the most recent healthy probe.
      - ``consecutive_fails``: running count of failed probes (reset on healthy).
      - ``outage_started``: monotonic timestamp when the outage clock started
        (set only once ``consecutive_fails`` reaches the threshold).

    On transition healthy→degraded (past threshold) it calls
    ``handle_dependency_health_payload``; on degraded→healthy it calls
    ``handle_dependency_recovered_payload`` with the elapsed outage duration.
    """

    def __init__(
        self,
        policies: list[DependencyHealthPolicy],
        *,
        probe_fn: ProbeFn | None = None,
        consecutive_fail_threshold: int = _DEFAULT_CONSECUTIVE_FAIL_THRESHOLD,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policies = {p.dependency_id: p for p in policies}
        self._probe_fn = probe_fn
        self._threshold = consecutive_fail_threshold
        self._clock = clock
        self._last_healthy: dict[str, float] = {}
        self._consecutive_fails: dict[str, int] = {}
        self._outage_started: dict[str, float] = {}
        # Seed the handler's policy registry so handle_*_payload can look up
        # policies by dependency_id without each payload carrying one.
        set_dependency_policies(policies)

    async def probe_all(self) -> list[dict[str, object]]:
        """Probe every registered dependency once and return per-dep results.

        Results are observability descriptors (dependency_id, reachable, and
        either ``outage_seconds`` on a routed degraded tick or ``recovered`` on
        a recovery tick); the actual alert routing happens inside the handler.
        """
        return [await self._probe_one(policy) for policy in self._policies.values()]

    async def _probe_one(self, policy: DependencyHealthPolicy) -> dict[str, object]:
        dependency_id = policy.dependency_id
        reachable = await self._probe(policy)
        now = self._clock()

        if reachable:
            was_degraded = dependency_id in self._outage_started
            self._consecutive_fails[dependency_id] = 0
            self._last_healthy[dependency_id] = now
            if was_degraded:
                outage_start = self._outage_started.pop(dependency_id)
                previous_outage = max(0.0, now - outage_start)
                handle_dependency_recovered_payload(
                    {"dependency_id": dependency_id, "previous_outage_seconds": previous_outage}
                )
                return {"dependency_id": dependency_id, "reachable": True, "recovered": True}
            return {"dependency_id": dependency_id, "reachable": True}

        # Failed probe: advance the consecutive-failure count, and only open
        # the outage clock once the threshold is met.
        self._last_healthy.pop(dependency_id, None)
        fails = self._consecutive_fails.get(dependency_id, 0) + 1
        self._consecutive_fails[dependency_id] = fails
        if fails < self._threshold:
            return {"dependency_id": dependency_id, "reachable": False, "below_threshold": True}

        if dependency_id not in self._outage_started:
            self._outage_started[dependency_id] = now
        current_outage = max(0.0, now - self._outage_started[dependency_id])
        handle_dependency_health_payload({"dependency_id": dependency_id, "current_outage_seconds": current_outage})
        return {
            "dependency_id": dependency_id,
            "reachable": False,
            "outage_seconds": current_outage,
        }

    async def _probe(self, policy: DependencyHealthPolicy) -> bool:
        if self._probe_fn is not None:
            return await self._probe_fn(policy)
        return await self._dispatch(policy)

    async def _dispatch(self, policy: DependencyHealthPolicy) -> bool:
        """Dispatch a probe on the policy's ``test_method``.

        All built-in methods currently report HEALTHY (fail-open) because the
        policies carry no probe target (endpoint/host/url/topic). ``synthetic_probe``
        is the default method for 20 of 27 policies and is a placeholder by
        design; the five concrete methods (``healthcheck_endpoint``,
        ``probe_ping``, ``probe_query``, ``probe_read_write``,
        ``probe_publish_subscribe``) need per-target config before a real probe
        can be built. Wire a real ``probe_fn`` (or extend this dispatch) once
        those targets exist.
        """
        method = policy.test_method
        logger.debug(
            "dependency probe: dependency_id=%s method=%s (scaffold — reporting healthy)",
            policy.dependency_id,
            method,
        )
        return True
