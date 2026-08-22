"""Anti-inertness guards for the dependency-health chain.

Issue: ``/plans/active/issues/live_path_has_no_stale_producer_revocation_2026_08_14.md``

The third of three such guards in the fleet. The other two
(``test_actuate_has_a_production_caller``, ``test_release_has_a_production_caller``
in deployment-service) exist because the batch revocation mechanism shipped
across six green phases with no production caller at all — every component
complete, tested, and unreachable.

The dependency-health chain has the identical defect and two layers more of it:

* ``DependencyHealthProber`` is instantiated ONLY in tests.
* ``probe_fn`` — the injection point for a real probe — has no production caller,
  and the built-in per-method probes report healthy by default (fail-open, by
  their own module docstring), so nothing can ever report unhealthy.
* Nothing EMITS ``DEPENDENCY_DEGRADED`` — not even the prober, which calls itself
  "the missing half" of it — so the fully-wired consumer half
  (``alert_subscriber`` -> ``handle_dependency_health_payload`` ->
  ``evaluate_dependency_health``) has nothing to consume.
* Even a fully-wired CRITICAL alert only pages: ``handle_dependency_health_payload``
  routes every dependency alert through ``route_event_with_explicit_channels``
  (PagerDuty/Telegram only) — never ``route_event``, the only path that calls
  ``publish_kill_switch_event``. No ``DEPENDENCY_DEGRADED*`` rule_id is registered
  in UAC's ``LIVE_ALERT_RULES`` either, so routing through ``route_event`` would
  not arm anything today regardless. A silently-dead strategy-service can page a
  human forever without the live path ever taking a protective action.

Each layer passes review because each layer genuinely IS finished. Checkbox
completeness cannot see this class of defect; a guard that asserts the component
has a live caller can, and it belongs next to the component rather than in a
checklist somebody has to remember to run.

All three guards below are ``xfail(strict=True)`` — they describe a KNOWN-inert
state. Strict is the load-bearing part: the moment one is genuinely wired the
test PASSES, strict turns that pass into a failure, and whoever wired it is
forced to delete the marker in the same change. That is exactly how the batch
guard behaved when its call site landed. Do NOT relax strict, and do not delete
a marker without landing the wiring it describes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parents[2] / "alerting_service"


def _calls_named(attr: str) -> list[str]:
    """Modules under ``alerting_service`` containing a real call to ``attr``.

    AST, not grep: a text search for ``probe_fn(`` or ``DEPENDENCY_DEGRADED``
    matches docstrings and constants, so prose could satisfy a grep-based guard
    while the wiring stayed absent — which is precisely the failure mode being
    guarded against.
    """
    found: list[str] = []
    for path in _SERVICE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == attr:
                    found.append(path.name)
                    break
    return found


def test_the_prober_runs_in_production() -> None:
    """Something outside tests must actually construct and run the prober.

    A policy nothing evaluates is indistinguishable from no policy. Fixed
    2026-08-22 (live_path_has_no_stale_producer_revocation_2026_08_14.md item
    1): ``dependency_health_runner.py`` constructs ``DependencyHealthProber``
    in production, wired into ``main.py``'s live-mode background tasks. No
    longer xfail — a regression here (the construction call removed/renamed)
    must fail CI, not silently pass as "expected broken".
    """
    assert _calls_named("DependencyHealthProber"), (
        "DependencyHealthProber is never constructed outside tests — every "
        "registered dependency reports healthy forever and no dependency-health "
        "alert can fire."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN INERT — the prober calls itself 'the missing half of DEPENDENCY_DEGRADED' "
        "but never emits it, so even a failing probe produces no event for the wired "
        "consumer. Tracked in live_path_has_no_stale_producer_revocation_2026_08_14.md. "
        "Remove this marker in the same change that makes the prober emit."
    ),
)
def test_the_prober_emits_the_event_its_consumer_waits_for() -> None:
    """A failing probe must produce ``DEPENDENCY_DEGRADED``.

    The consumer half is complete and correct: ``alert_subscriber`` routes the
    event to ``handle_dependency_health_payload``, which evaluates the policy and
    fires graded alerts. It has never run because nothing emits. The prober is
    the intended producer — its own module docstring calls it "the missing half
    of ``DEPENDENCY_DEGRADED``" — and it does not emit.

    Matched on an EMIT-shaped call specifically, not on the string appearing
    anywhere: the first version of this guard accepted any call carrying the
    constant and was satisfied by ``details.get("DEPENDENCY_DEGRADED", ...)``, a
    fallback default inside the CONSUMER. It passed while the producer was still
    missing — the precise failure this guard family exists to catch, reproduced
    inside the guard itself.
    """
    emit_names = {"log_event", "emit", "publish", "emit_event", "log_alert"}
    source = (_SERVICE_ROOT / "dependency_health_prober.py").read_text(encoding="utf-8")
    emits = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) in emit_names
        and any(isinstance(a, ast.Constant) and a.value == "DEPENDENCY_DEGRADED" for a in node.args)
    ]
    assert emits, (
        "The prober never emits DEPENDENCY_DEGRADED, so its fully-wired consumer "
        "has nothing to consume — the fleet is listening and nobody speaks."
    )


def test_a_critical_dependency_alert_reaches_an_actuator_not_only_a_channel() -> None:
    """A CRITICAL dependency-health alert must change behaviour, not just page.

    Fixed 2026-08-22 (live_path_has_no_stale_producer_revocation_2026_08_14.md
    item 1c): ``dependency_health_event_handler._maybe_arm_kill_switch`` calls
    ``get_kill_switch_bus()`` for a CRITICAL alert whose policy carries a
    ``kill_switch_scope`` (execution-service, strategy-service — see
    ``dependency_health_policies.yaml``); every other registered dependency
    stays alert-only (``kill_switch_scope=None``), unchanged. No longer xfail —
    a regression back to alert-only must fail CI. Scoped to this ONE file
    deliberately, not a whole-tree ``_calls_named`` search: ``get_kill_switch_bus``
    / ``publish_kill_switch_event`` are already called elsewhere in
    alerting-service for OTHER alert families, so a tree-wide search would pass
    for the wrong reason — the exact false-negative shape the emit guard above
    already learned from once.
    """
    actuator_names = {"publish_kill_switch_event", "_publish_kill_switch_event", "get_kill_switch_bus"}
    source = (_SERVICE_ROOT / "dependency_health_event_handler.py").read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) in actuator_names
    ]
    assert calls, (
        "dependency_health_event_handler.py never calls an actuator that changes "
        "behaviour (publish_kill_switch_event / get_kill_switch_bus) — a CRITICAL "
        "dependency outage only pages today; nothing on the live path halts or "
        "protects a position because an internal dependency went SEV0."
    )


def test_all_guards_are_strict() -> None:
    """The remaining marker(s) must stay ``strict``, or they stop being self-removing.

    A non-strict xfail silently absorbs a pass, so wiring the chain would leave
    the marker in place claiming the system is still broken — the doc-rot this
    whole guard family exists to prevent. This asserts the property directly
    rather than trusting a comment. ``test_the_prober_runs_in_production`` and
    ``test_a_critical_dependency_alert_reaches_an_actuator_not_only_a_channel``
    dropped out of this tuple 2026-08-22 once their xfail markers were removed
    (the wiring they guarded now exists) — only the still-inert producer-emit
    guard remains.
    """
    guarded = (test_the_prober_emits_the_event_its_consumer_waits_for,)
    for fn in guarded:
        marks = [m for m in fn.pytestmark if m.name == "xfail"]
        assert marks, f"{fn.__name__} lost its xfail marker"
        assert marks[0].kwargs.get("strict") is True, f"{fn.__name__}'s xfail must stay strict"
