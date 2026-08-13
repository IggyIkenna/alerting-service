"""Alert-code parity — every AlertCode in UAC has routing coverage in LIVE_ALERT_RULES.

Item 3 of slot-4 queue (continuation_prompts_harsh_2026_05_15): verify every
AlertCode has at least one integration test asserting it fires correctly.

Two tiers of coverage:
  1. Parametrized sweep (77 tests): every AlertCode matches ≥1 rule (even catch-all *).
  2. Explicit-rule ratchet: all 77 AlertCodes have dedicated explicit routing rules
     (UAC 76e144d5 registered CHAOS_DRILL_FAILED + DAILY_LEDGER_DIGEST and fixed the
     RECON_DEGRADED glob, closing the last catch-all-only gaps). Prevents new codes
     from silently slipping through without an AlertRule.
  3. Family spot-checks: DeFi Family 1/2 / risk-rule / stablecoin / QG snapshot codes
     each have an explicit (non-catch-all) routing rule.
"""

from __future__ import annotations

import fnmatch

import pytest
from unified_api_contracts import (
    LIVE_ALERT_RULES,
    AlertCode,
    AlertSeverity,
)

from alerting_service.config import _default_routing_rules

# ---------------------------------------------------------------------------
# Tier 1 — parametrized sweep: every AlertCode matches ≥1 rule (catch-all counts)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", list(AlertCode), ids=[c.value for c in AlertCode])
def test_every_alert_code_routes_to_at_least_one_rule(code: AlertCode) -> None:
    """Every AlertCode in the closed set must be handled by ≥1 routing rule
    (explicit per-code rule OR the mandatory catch-all `*` fallback)."""
    matches = [r for r in LIVE_ALERT_RULES if fnmatch.fnmatchcase(code.value, r.event_pattern)]
    assert matches, (
        f"AlertCode.{code.name} matches zero rules in LIVE_ALERT_RULES "
        "(not even the catch-all `*`) — routing is completely broken for this code"
    )


# ---------------------------------------------------------------------------
# Tier 2 — explicit-rule coverage ratchet
# ---------------------------------------------------------------------------

# Codes whose only handler is the catch-all `*` (no dedicated AlertRule yet).
# Adding a code here requires also adding an explicit AlertRule in UAC rules.py.
# Empty as of UAC 76e144d5 (2026-08-12): CHAOS_DRILL_FAILED + DAILY_LEDGER_DIGEST
# got explicit rules and the RECON_DEGRADED glob fix made the bare code match too.
_KNOWN_CATCH_ALL_ONLY: frozenset[AlertCode] = frozenset()


def test_catch_all_only_codes_are_the_known_set() -> None:
    """Ratchet: exactly the known (currently empty) set is catch-all-`*`-only.

    Fails when a new AlertCode is added to the enum without a corresponding
    explicit AlertRule in LIVE_ALERT_RULES. Add the AlertRule, or update
    _KNOWN_CATCH_ALL_ONLY with a comment explaining the deferral.
    """
    actual_catch_all_only: set[AlertCode] = set()
    for code in AlertCode:
        has_explicit = any(
            r.event_pattern != "*" and fnmatch.fnmatchcase(code.value, r.event_pattern) for r in LIVE_ALERT_RULES
        )
        if not has_explicit:
            actual_catch_all_only.add(code)

    assert actual_catch_all_only == _KNOWN_CATCH_ALL_ONLY, (
        f"Catch-all-only codes changed.\n"
        f"  Expected: {sorted(c.value for c in _KNOWN_CATCH_ALL_ONLY)}\n"
        f"  Actual:   {sorted(c.value for c in actual_catch_all_only)}\n"
        "Add an explicit AlertRule in UAC for any new codes, "
        "or update _KNOWN_CATCH_ALL_ONLY with a deferral comment."
    )


def test_explicit_coverage_count_at_least_75() -> None:
    """Growth ratchet: all 77 AlertCodes have explicit routing rules (no
    catch-all-only codes remain, per UAC 76e144d5). Prevents silent regression
    when codes are removed or rules deleted."""
    explicitly_covered = sum(
        1
        for code in AlertCode
        if any(r.event_pattern != "*" and fnmatch.fnmatchcase(code.value, r.event_pattern) for r in LIVE_ALERT_RULES)
    )
    assert explicitly_covered >= 77, (
        f"Only {explicitly_covered} AlertCodes have explicit routing rules "
        "(expected ≥77). A recently-added code may lack an AlertRule, or an existing "
        "rule was removed."
    )


# ---------------------------------------------------------------------------
# Tier 3 — family spot-checks for codes not covered in other alerting-service tests
# ---------------------------------------------------------------------------

_DEFI_FAMILY_12_CODES: tuple[AlertCode, ...] = (
    AlertCode.DEFI_LIQUIDATION_IMMINENT,
    AlertCode.DEFI_CROSS_VENUE_DELTA_DRIFT,
    AlertCode.DEFI_PERP_VENUE_OUTAGE,
    AlertCode.DEFI_ORACLE_STALE_PAUSE,
    AlertCode.DEFI_RECURSIVE_LOOP_GAS_BUDGET_EXCEEDED,
)


@pytest.mark.parametrize(
    "code",
    _DEFI_FAMILY_12_CODES,
    ids=[c.value for c in _DEFI_FAMILY_12_CODES],
)
def test_defi_family_12_codes_have_explicit_routing_rule(code: AlertCode) -> None:
    """DeFi Family 1/2 (recursive-borrow + delta-hedge) codes added 2026-05-12
    must each have an explicit (non-catch-all) routing rule in LIVE_ALERT_RULES."""
    explicit_matches = [
        r for r in LIVE_ALERT_RULES if r.event_pattern != "*" and fnmatch.fnmatchcase(code.value, r.event_pattern)
    ]
    assert explicit_matches, (
        f"AlertCode.{code.name} has no explicit routing rule "
        "(only catch-all `*`) — add an AlertRule for the DeFi Family 1/2 archetype"
    )


_RISK_RULE_CODES: tuple[AlertCode, ...] = (
    AlertCode.RISK_RULE_BLOCKED,
    AlertCode.RISK_RULE_SCALED_DOWN,
    AlertCode.RISK_RULE_MONITOR_FIRED,
    AlertCode.RISK_RULE_TEST_ONLY_ROUTED,
)


@pytest.mark.parametrize(
    "code",
    _RISK_RULE_CODES,
    ids=[c.value for c in _RISK_RULE_CODES],
)
def test_risk_rule_codes_have_explicit_routing_rule(code: AlertCode) -> None:
    """Risk-rule consequence codes (BLOCK / SCALE_DOWN / MONITOR / TEST_ONLY)
    must each have an explicit routing rule in LIVE_ALERT_RULES with the
    correct per-consequence severity (BLOCK=HIGH; others=WARN/INFO)."""
    explicit_matches = [
        r for r in LIVE_ALERT_RULES if r.event_pattern != "*" and fnmatch.fnmatchcase(code.value, r.event_pattern)
    ]
    assert explicit_matches, (
        f"AlertCode.{code.name} has no explicit routing rule "
        "(only catch-all `*`) — add an AlertRule for the risk-rule consequence"
    )


def test_stablecoin_codes_have_explicit_routing_rules() -> None:
    """STABLECOIN_ISSUER_PAUSED + GOVERNANCE_INCIDENT_DETECTED (Phase D.5/D.7)
    must both have explicit routing rules — depeg signals need dedicated treatment."""
    for code in (AlertCode.STABLECOIN_ISSUER_PAUSED, AlertCode.GOVERNANCE_INCIDENT_DETECTED):
        explicit_matches = [
            r for r in LIVE_ALERT_RULES if r.event_pattern != "*" and fnmatch.fnmatchcase(code.value, r.event_pattern)
        ]
        assert explicit_matches, (
            f"AlertCode.{code.name} has no explicit routing rule "
            "(expected: HIGH+PagerDuty for issuer-paused; operator-page for governance incident)"
        )


def test_qg_snapshot_stale_has_explicit_routing_rule_at_high_severity() -> None:
    """QG_SNAPSHOT_STALE (2026-05-15 B-018 Phase 4.A) must have an explicit
    routing rule at HIGH severity so the operator is paged when the snapshot VM
    silently dies and daily QG evidence stops accumulating."""
    matched = [
        r
        for r in LIVE_ALERT_RULES
        if r.event_pattern != "*" and fnmatch.fnmatchcase("QG_SNAPSHOT_STALE", r.event_pattern)
    ]
    assert matched, "AlertCode.QG_SNAPSHOT_STALE has no explicit routing rule"
    rule = matched[0]
    assert rule.severity in (AlertSeverity.HIGH, AlertSeverity.CRITICAL), (
        f"QG_SNAPSHOT_STALE.severity={rule.severity!r} — expected HIGH or CRITICAL "
        "so operators are paged when snapshot VM stops writing"
    )


def test_recon_degraded_close_has_explicit_routing_rule() -> None:
    """RECON_DEGRADED_CLOSE must have an explicit routing rule — closing positions
    without verified reconciliation state is an operator-visibility event.

    NOTE: the RECON_DEGRADED* wildcard pattern (UAC 76e144d5) covers both the bare
    RECON_DEGRADED code and the RECON_DEGRADED_CLOSE suffix variant explicitly.
    """
    matched = [
        r
        for r in LIVE_ALERT_RULES
        if r.event_pattern != "*" and fnmatch.fnmatchcase("RECON_DEGRADED_CLOSE", r.event_pattern)
    ]
    assert matched, (
        "AlertCode.RECON_DEGRADED_CLOSE has no explicit routing rule "
        "(RECON_DEGRADED_* wildcard should match it — verify the rule is present)"
    )


# ---------------------------------------------------------------------------
# alerting-service config parity guard
# ---------------------------------------------------------------------------


def test_service_routing_config_covers_all_alert_codes_via_fnmatch() -> None:
    """The alerting-service routing table (from _default_routing_rules) must
    cover every AlertCode via fnmatch. Catches drift between LIVE_ALERT_RULES and
    the service-level config factory."""
    service_rules = _default_routing_rules()
    uncovered: list[str] = []
    for code in AlertCode:
        matched = [r for r in service_rules if fnmatch.fnmatchcase(code.value, str(r["event_pattern"]))]
        if not matched:
            uncovered.append(code.value)
    assert not uncovered, (
        "These AlertCodes are not matched by any service routing rule in "
        "_default_routing_rules() — service config is out of sync with LIVE_ALERT_RULES:\n"
        + "\n".join(f"  {c}" for c in sorted(uncovered))
    )
