"""Connectivity + dependency health alert evaluation rules.

Evaluates dependency outage events against per-dependency health policies
(``DependencyHealthPolicy`` from UAC) using the expected_time + buffer
escalation ladder:

  outage <= expected_recovery_time            → no alert
  outage > expected + warning_buffer          → WARNING  (Slack)
  outage > expected + warning + human_invest  → WARNING  + sev1_escalate (SEV1)
  outage > hard_escalation                    → CRITICAL (SEV0, PagerDuty + Telegram)
  no fallback AND outage >= expected AND >= N consecutive failed probes
                                               → CRITICAL (SEV0) — "no fallback" raises
                                                 severity, never bypasses duration

STATUS (verified 2026-08-12): NOT WIRED. This module is imported by nothing —
no handler computes ``current_outage_seconds`` and no subscriber calls these
rules, so ``DEPENDENCY_DEGRADED`` cannot currently fire. The unit tests pass by
calling the functions directly with synthetic values, which proves the ladder's
arithmetic and nothing about reachability. Do not read a green test here as
evidence that dependency alerting works.

Input contract: callers pass ``event_details`` with ``dependency_id`` and
``current_outage_seconds``, plus the matching policy. An earlier version of this
docstring named a ``CONNECTIVITY_DEGRADED`` event as the source; **no such event
type exists anywhere in the fleet** — it was never built. The agreed producer is
a probe driven by each policy's ``test_method`` field; see the issue doc below.

Issue: ``/plans/active/issues/dependency_health_alerting_never_wired_2026_08_12.md``
Plan (archived): ``/plans/archive/2026_05/connectivity_dependency_buffer_policy_2026_05_23.md``
"""

from __future__ import annotations

from datetime import UTC, datetime

from unified_api_contracts.alerting import AlertSeverity
from unified_api_contracts.dependency import DependencyHealthPolicy

DEPENDENCY_DEGRADED_MIN_CONSECUTIVE_FAILURES: int = 3
"""Minimum consecutive failed probes required before the no-fallback branch may
raise an outage to CRITICAL (SEV0).

"no fallback" raises SEVERITY, it never bypasses DURATION: a single flaky probe
(or any outage shorter than ``expected_recovery_time_seconds``) against a
dependency with ``fallback_available: false`` must not page. The no-fallback
severity-raise fires only once BOTH the duration floor is crossed AND the outage
is confirmed by this many consecutive failed probes. The producer (probe-driven,
tracked per ``dependency_id``) passes ``consecutive_failures`` in
``event_details``; while it is unwired, a missing counter defaults to 0 and the
no-fallback branch stays silent."""


def evaluate_dependency_health(
    event_details: dict[str, object],
    policy: DependencyHealthPolicy,
) -> list[dict[str, object]]:
    """Evaluate a dependency outage against its health policy.

    Args:
        event_details: Event payload. Required keys: ``dependency_id``,
            ``current_outage_seconds``. Optional: ``venue``, ``strategy_id``.
        policy: ``DependencyHealthPolicy`` for this dependency. Callers
            look up the policy from ``dependency_health_policies.yaml`` by
            ``dependency_id`` before calling.

    Returns:
        List of 0 or 1 alert dicts.
    """
    try:
        outage = float(str(event_details.get("current_outage_seconds", "0")))
    except (ValueError, TypeError):
        return []

    if outage <= 0:
        return []

    # Confirmed-failure count comes from the producer (probe-driven, tracked per
    # dependency_id). Missing/unparseable → 0, which leaves the no-fallback
    # branch silent — the SAFE default while the producer is unwired.
    try:
        consecutive_failures = int(str(event_details.get("consecutive_failures", "0")))
    except (ValueError, TypeError):
        consecutive_failures = 0

    expected = policy.expected_recovery_time_seconds
    warn_at = expected + policy.warning_buffer_seconds
    sev1_at = warn_at + policy.human_investigation_buffer_seconds

    # SEV0 conditions: hard ceiling breached (a duration floor on its own) OR the
    # no-fallback severity-raise — which requires BOTH the duration floor crossed
    # AND DEPENDENCY_DEGRADED_MIN_CONSECUTIVE_FAILURES consecutive failed probes.
    # "no fallback" raises SEVERITY, it never bypasses DURATION: a single flaky
    # probe against a fallback-less dependency must not page.
    no_fallback_confirmed = (
        not policy.fallback_available
        and outage >= expected
        and consecutive_failures >= DEPENDENCY_DEGRADED_MIN_CONSECUTIVE_FAILURES
    )
    if outage >= policy.hard_escalation_seconds or no_fallback_confirmed:
        severity: AlertSeverity = AlertSeverity.CRITICAL
        delivery_channel = "pagerduty+telegram"
        rule_id = "DEPENDENCY_DEGRADED_CRITICAL"
        sev1_escalate = False
    elif outage >= sev1_at:
        severity = AlertSeverity.WARN
        delivery_channel = "telegram"
        rule_id = "DEPENDENCY_DEGRADED_SEV1"
        sev1_escalate = True
    elif outage >= warn_at:
        severity = AlertSeverity.WARN
        delivery_channel = "telegram"
        rule_id = "DEPENDENCY_DEGRADED_WARN"
        sev1_escalate = False
    elif outage >= expected:
        # Between expected and warn_at — sub-warn band, no alert
        return []
    else:
        return []

    now = datetime.now(UTC)
    dependency_id = str(event_details.get("dependency_id", policy.dependency_id))
    return [
        {
            "alert_id": (f"dep-{dependency_id.lower()}-{now.strftime('%H%M%S')}"),
            "rule_id": rule_id,
            "metric_name": "dependency_outage_seconds",
            "metric_value": f"{outage:.0f}",
            "severity": severity,
            "dependency_id": dependency_id,
            "dependency_class": policy.dependency_class.value,
            "consecutive_failures": consecutive_failures,
            "venue": str(event_details.get("venue", "")),  # noqa: qg-empty-fallback
            "message": (
                f"Dependency {dependency_id} outage {outage:.0f}s "
                f"(class={policy.dependency_class.value}, "
                f"expected={expected}s, hard={policy.hard_escalation_seconds}s, "
                f"fallback={'yes' if policy.fallback_available else 'no'}, "
                f"consecutive_failures={consecutive_failures})"
            ),
            "created_at": now.isoformat(),
            "delivered": False,
            "delivery_channel": delivery_channel,
            "sev1_escalate": sev1_escalate,
            "runbook_doc": policy.runbook_doc,
        }
    ]


def evaluate_dependency_recovered(
    event_details: dict[str, object],
    policy: DependencyHealthPolicy,
) -> list[dict[str, object]]:
    """Emit a DEPENDENCY_RECOVERED informational alert when outage drops to 0.

    Args:
        event_details: Payload with ``dependency_id`` + ``previous_outage_seconds``.
        policy: Health policy for context fields.
    """
    previous_outage = float(str(event_details.get("previous_outage_seconds", "0")))
    if previous_outage <= 0:
        return []

    now = datetime.now(UTC)
    dependency_id = str(event_details.get("dependency_id", policy.dependency_id))
    return [
        {
            "alert_id": f"dep-recovered-{dependency_id.lower()}-{now.strftime('%H%M%S')}",
            "rule_id": "DEPENDENCY_RECOVERED",
            "metric_name": "dependency_outage_seconds",
            "metric_value": "0",
            "severity": "INFO",
            "dependency_id": dependency_id,
            "dependency_class": policy.dependency_class.value,
            "venue": str(event_details.get("venue", "")),  # noqa: qg-empty-fallback
            "message": (f"Dependency {dependency_id} recovered after {previous_outage:.0f}s outage"),
            "created_at": now.isoformat(),
            "delivered": False,
            "delivery_channel": "telegram",
            "sev1_escalate": False,
            "runbook_doc": policy.runbook_doc,
        }
    ]
