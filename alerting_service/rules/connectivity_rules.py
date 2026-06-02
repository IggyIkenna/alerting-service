"""Connectivity + dependency health alert evaluation rules.

Evaluates dependency outage events against per-dependency health policies
(``DependencyHealthPolicy`` from UAC) using the expected_time + buffer
escalation ladder:

  outage <= expected_recovery_time            → no alert
  outage > expected + warning_buffer          → WARNING  (Slack)
  outage > expected + warning + human_invest  → WARNING  + sev1_escalate (SEV1)
  outage > hard_escalation OR no-fallback     → CRITICAL (SEV0, PagerDuty + Telegram)

Events consumed:
- CONNECTIVITY_DEGRADED  — a dependency outage has been detected
- DEPENDENCY_RECOVERED   — emitted when outage_seconds drops to 0

Plan: ``plans/active/connectivity_dependency_buffer_policy_2026_05_23.md``
Phase 3 P0.6-P0.7.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from unified_api_contracts.dependency import DependencyHealthPolicy


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

    expected = policy.expected_recovery_time_seconds
    warn_at = expected + policy.warning_buffer_seconds
    sev1_at = warn_at + policy.human_investigation_buffer_seconds

    # SEV0 conditions: exceeded hard ceiling OR zero fallback available
    if outage >= policy.hard_escalation_seconds or not policy.fallback_available:
        severity: Literal["WARNING", "CRITICAL"] = "CRITICAL"
        delivery_channel = "pagerduty+telegram"
        rule_id = "DEPENDENCY_DEGRADED_CRITICAL"
        sev1_escalate = False
    elif outage >= sev1_at:
        severity = "WARNING"
        delivery_channel = "telegram"
        rule_id = "DEPENDENCY_DEGRADED_SEV1"
        sev1_escalate = True
    elif outage >= warn_at:
        severity = "WARNING"
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
            "venue": str(event_details.get("venue", "")),  # noqa: qg-empty-fallback
            "message": (
                f"Dependency {dependency_id} outage {outage:.0f}s "
                f"(class={policy.dependency_class.value}, "
                f"expected={expected}s, hard={policy.hard_escalation_seconds}s, "
                f"fallback={'yes' if policy.fallback_available else 'no'})"
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
