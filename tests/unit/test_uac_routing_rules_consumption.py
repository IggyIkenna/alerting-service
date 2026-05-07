"""Phase 2 of `alerting_service_live_rules_2026_05_07` — verify alerting-service
consumes the UAC `LIVE_ALERT_RULES` SSOT instead of an inline default-factory.

Closed-set parity checks:
- ``alerting_service.config._default_routing_rules()`` is byte-equivalent to
  ``[r.to_routing_dict() for r in LIVE_ALERT_RULES]``.
- ``check_aave_utilization`` reads the threshold from
  ``unified_api_contracts.alerting.ALERT_THRESHOLDS`` and respects per-archetype
  overrides.
- KILL_SWITCH_* family rules carry CRITICAL severity + PagerDuty channel.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from unified_api_contracts import (
    ALERT_THRESHOLDS,
    LIVE_ALERT_RULES,
    AlertChannel,
    AlertCode,
    AlertSeverity,
)

from alerting_service.config import _default_routing_rules
from alerting_service.rules.defi_rules import (
    _aave_utilization_threshold_ratio,
    check_aave_utilization,
)

# ---------------------------------------------------------------------------
# config._default_routing_rules ↔ UAC LIVE_ALERT_RULES parity
# ---------------------------------------------------------------------------


def test_default_routing_rules_matches_uac_live_alert_rules_byte_for_byte() -> None:
    """alerting-service config.py no longer holds a parallel SSOT — the
    default-factory is a pure render of UAC LIVE_ALERT_RULES."""
    expected = [rule.to_routing_dict() for rule in LIVE_ALERT_RULES]
    actual = _default_routing_rules()
    assert actual == expected


def test_default_routing_rules_count_matches_uac_rule_count() -> None:
    rules = _default_routing_rules()
    assert len(rules) == len(LIVE_ALERT_RULES)


def test_default_routing_rules_catch_all_is_last() -> None:
    """fnmatch first-match-wins requires the catch-all `*` rule to be last."""
    rules = _default_routing_rules()
    assert rules[-1]["event_pattern"] == "*", (
        "catch-all `*` rule must be last; otherwise specific rules never match"
    )


def test_default_routing_rules_have_legacy_shape() -> None:
    """Every rule has exactly the three legacy fields — drift between
    LIVE_ALERT_RULES.to_routing_dict() and the legacy default-factory shape
    is a regression."""
    rules = _default_routing_rules()
    for rule in rules:
        assert set(rule.keys()) == {"event_pattern", "channels", "severity_filter"}
        assert isinstance(rule["event_pattern"], str)
        assert isinstance(rule["channels"], list)
        assert rule["severity_filter"] in {"critical", "warning", None}


# ---------------------------------------------------------------------------
# Severity / channel coverage sanity
# ---------------------------------------------------------------------------


def test_kill_switch_rules_are_critical_with_pagerduty() -> None:
    """KILL_SWITCH_* family must page PagerDuty + carry CRITICAL severity so
    on-call is alerted within SLA."""
    kill_switch_rules = [
        rule for rule in LIVE_ALERT_RULES if rule.code.value.startswith("KILL_SWITCH_")
    ]
    assert kill_switch_rules, "KILL_SWITCH_* family must have at least one rule"
    for rule in kill_switch_rules:
        assert rule.severity is AlertSeverity.CRITICAL
        assert AlertChannel.PAGERDUTY in rule.channels
        assert rule.triggers_kill_switch is True


def test_cross_cloud_egress_detected_routed_to_pagerduty() -> None:
    """Audit 2026-05-07 §dual-cloud-active: the safety-net AlertCode must
    page on-call — a UI in cloud A reading data from cloud B is a bug."""
    cross_cloud_rules = [
        rule for rule in LIVE_ALERT_RULES if rule.code is AlertCode.CROSS_CLOUD_EGRESS_DETECTED
    ]
    assert cross_cloud_rules, "CROSS_CLOUD_EGRESS_DETECTED must have a rule"
    for rule in cross_cloud_rules:
        assert AlertChannel.PAGERDUTY in rule.channels


# ---------------------------------------------------------------------------
# AAVE threshold migration to UAC
# ---------------------------------------------------------------------------


def test_aave_utilization_threshold_ratio_default_95_percent() -> None:
    """UAC `defi_aave_utilization_spike_bps` = 9500 bps_of_one (95.00%).
    Audit 2026-05-07 §3 #5 ambiguity is resolved by explicit BPS_OF_ONE unit."""
    assert _aave_utilization_threshold_ratio(None) == Decimal("0.95")
    assert _aave_utilization_threshold_ratio("carry_staked_basis") == Decimal("0.95")


def test_aave_utilization_threshold_per_archetype_override() -> None:
    """leveraged_funding_arb gets a tighter 90% threshold per UAC override."""
    assert _aave_utilization_threshold_ratio("leveraged_funding_arb") == Decimal("0.90")


def test_aave_utilization_threshold_unknown_archetype_falls_back() -> None:
    assert _aave_utilization_threshold_ratio("unknown_archetype") == Decimal("0.95")


def test_check_aave_utilization_below_threshold_returns_none() -> None:
    alert = check_aave_utilization(
        utilization_rate=Decimal("0.94"),
        pool_name="USDC",
        protocol="aave_v3",
    )
    assert alert is None


def test_check_aave_utilization_above_threshold_returns_alert() -> None:
    alert = check_aave_utilization(
        utilization_rate=Decimal("0.96"),
        pool_name="USDC",
        protocol="aave_v3",
    )
    assert alert is not None
    assert alert.alert_type.value == "aave_utilization_spike"
    assert alert.protocol == "aave_v3"
    assert alert.asset == "USDC"
    assert alert.details["threshold_ratio"] == 0.95
    assert alert.details["archetype"] == "default"


def test_check_aave_utilization_per_archetype_override() -> None:
    """leveraged_funding_arb fires earlier (above 90%); 0.91 wouldn't fire
    on default but does fire under the tighter override."""
    default_alert = check_aave_utilization(
        utilization_rate=Decimal("0.91"),
        pool_name="USDC",
        protocol="aave_v3",
    )
    archetype_alert = check_aave_utilization(
        utilization_rate=Decimal("0.91"),
        pool_name="USDC",
        protocol="aave_v3",
        archetype="leveraged_funding_arb",
    )
    assert default_alert is None
    assert archetype_alert is not None
    assert archetype_alert.details["threshold_ratio"] == 0.90
    assert archetype_alert.details["archetype"] == "leveraged_funding_arb"


# ---------------------------------------------------------------------------
# Smoke test — UAC rules cover all the legacy patterns
# ---------------------------------------------------------------------------


_LEGACY_PATTERNS_MUST_BE_PRESENT: tuple[str, ...] = (
    "KILL_SWITCH_*",
    "CIRCUIT_BREAKER_OPEN",
    "UNHEDGED_POSITION_ALERT",
    "MULTI_LEG_COMPENSATION_FAILED",
    "DUAL_FAILURE_DETECTED",
    "ORDER_RECOVERY_FAILED",
    "SERVICE_ERROR_CRITICAL",
    "DEFI_HEALTH_FACTOR_CRITICAL",
    "DEFI_WEETH_DEPEG",
    "MARGIN_LIQUIDATION",
    "MARGIN_CRITICAL",
    "ORDER_ORPHANED",
    "POSITION_DRIFT_DETECTED",
    "POSITION_CRITICAL_DISCREPANCY",
    "MARGIN_WARNING",
    "CIRCUIT_BREAKER_DEGRADED",
    "CIRCUIT_BREAKER_CLOSED",
    "SERVICE_ERROR",
    "PREFLIGHT_FAILED",
    "SERVICE_DEGRADED",
    "DEFI_AAVE_UTILIZATION_SPIKE",
    "DEFI_FUNDING_RATE_FLIP",
    "DEFI_FEATURE_STALE",
    "*",
)


@pytest.mark.parametrize("pattern", _LEGACY_PATTERNS_MUST_BE_PRESENT)
def test_legacy_routing_pattern_present_in_uac(pattern: str) -> None:
    """Drift guard — every pattern that lived in the pre-Phase-2 inline
    default-factory must be present in UAC LIVE_ALERT_RULES so the
    migration is byte-equivalent for routing behaviour."""
    routing_dicts = _default_routing_rules()
    patterns = {entry["event_pattern"] for entry in routing_dicts}
    assert pattern in patterns, (
        f"legacy pattern {pattern!r} dropped during UAC migration — "
        "alerting-service Phase 2 is incomplete"
    )


# ---------------------------------------------------------------------------
# UAC threshold registry consumption
# ---------------------------------------------------------------------------


def test_aave_threshold_unit_is_bps_of_one() -> None:
    """Phase 2 requires unit consistency between UAC + alerting-service —
    if UAC ever ships a non-bps unit, alerting-service must adapt."""
    threshold = ALERT_THRESHOLDS["defi_aave_utilization_spike_bps"]
    assert threshold.unit.value == "bps_of_one"
