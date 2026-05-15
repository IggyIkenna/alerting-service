"""Unit tests for ``dr_event_handler`` — Phase 4.C (alerting half) +
Phase 5.A recovery AlertCodes of ``disaster_recovery_circuit_breakers_2026_05_10``.

Coverage:
- ``KillSwitchArmedEvent`` → CRITICAL+page for global/wallet/asset-group switches,
  HIGH+page otherwise.
- ``KillSwitchDisarmEvent`` → ``KILL_SWITCH_AUTO_RECOVERED`` (AUTO_COOLDOWN) /
  ``KILL_SWITCH_MANUAL_UNKILLED`` (MANUAL_UNKILL), both INFO → Telegram.
- Circuit-breaker fire → severity tier per ``BreakerAction`` (KILL_ALL→CRITICAL+page,
  CANCEL_OPEN→HIGH+page, SCALE_DOWN/BLOCK_NEW→WARN→Telegram).
- Malformed payloads dropped, not raised.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from unified_api_contracts import (
    AlertSeverity,
    BreakerAction,
    BreakerRecoveryMode,
    CircuitBreakerId,
    KillSwitchArmedEvent,
    KillSwitchDisarmEvent,
    KillSwitchId,
    KillSwitchProvenance,
)
from unified_api_contracts.alerting import AlertCode
from unified_api_contracts.registry.circuit_breakers import PER_ARCHETYPE_BREAKERS

from alerting_service.dr_event_handler import (
    _breaker_action_for,
    _severity_channels,
    handle_circuit_breaker_fire,
    handle_circuit_breaker_fire_payload,
    handle_kill_switch_armed_event,
    handle_kill_switch_armed_payload,
    handle_kill_switch_disarm_event,
    handle_kill_switch_disarm_payload,
)


def _armed(switch_id: KillSwitchId) -> KillSwitchArmedEvent:
    return KillSwitchArmedEvent(
        switch_id=switch_id,
        provenance=KillSwitchProvenance.BREAKER_AUTO,
        armed_at=datetime.now(UTC),
        requested_by="breaker",
        target_wallet_id="w1",
        metadata={"breaker": "DRAWDOWN_DAILY_BPS"},
    )


def _disarm(mode: BreakerRecoveryMode, *, elapsed: int | None = 300) -> KillSwitchDisarmEvent:
    return KillSwitchDisarmEvent(
        switch_id=KillSwitchId.KILL_ALL_LIVE,
        target_wallet_id="w1",
        disarmed_at=datetime.now(UTC),
        disarmed_by="op-7",
        recovery_mode=mode,
        cooldown_seconds_elapsed=elapsed,
        metadata={},
    )


# ---------------------------------------------------------------------------
# severity → channel helper
# ---------------------------------------------------------------------------
class TestSeverityChannels:
    def test_critical_pages(self) -> None:
        channels, pd_sev = _severity_channels(AlertSeverity.CRITICAL)
        assert channels == {"pagerduty", "telegram"}
        assert pd_sev == "critical"

    def test_high_pages_warning_severity(self) -> None:
        channels, pd_sev = _severity_channels(AlertSeverity.HIGH)
        assert channels == {"pagerduty", "telegram"}
        assert pd_sev == "warning"

    def test_warn_is_telegram_only(self) -> None:
        channels, pd_sev = _severity_channels(AlertSeverity.WARN)
        assert channels == {"telegram"}
        assert pd_sev is None

    def test_info_is_telegram_only(self) -> None:
        channels, pd_sev = _severity_channels(AlertSeverity.INFO)
        assert channels == {"telegram"}
        assert pd_sev is None


# ---------------------------------------------------------------------------
# KillSwitchArmedEvent
# ---------------------------------------------------------------------------
_CRITICAL_SWITCHES = [
    KillSwitchId.KILL_ALL_LIVE,
    KillSwitchId.KILL_PER_WALLET,
    KillSwitchId.KILL_PER_ASSET_GROUP_CEFI,
    KillSwitchId.KILL_PER_ASSET_GROUP_DEFI,
]
_HIGH_SWITCHES = [
    KillSwitchId.KILL_PER_VENUE_BYBIT,
    KillSwitchId.KILL_PER_ARCHETYPE_CARRY_STAKED_BASIS,
]


class TestKillSwitchArmed:
    @pytest.mark.parametrize("switch_id", _CRITICAL_SWITCHES)
    def test_critical_arm_pages_critical(self, switch_id: KillSwitchId) -> None:
        with patch("alerting_service.dr_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_kill_switch_armed_event(_armed(switch_id))
        mock_route.assert_called_once()
        kwargs = mock_route.call_args.kwargs
        assert kwargs["channels"] == {"pagerduty", "telegram"}
        assert kwargs["pd_severity"] == "critical"
        _name, details = mock_route.call_args[0]
        assert details["severity"] == AlertSeverity.CRITICAL.value
        assert details["switch_id"] == switch_id.value

    @pytest.mark.parametrize("switch_id", _HIGH_SWITCHES)
    def test_scoped_arm_pages_high(self, switch_id: KillSwitchId) -> None:
        with patch("alerting_service.dr_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_kill_switch_armed_event(_armed(switch_id))
        kwargs = mock_route.call_args.kwargs
        assert kwargs["channels"] == {"pagerduty", "telegram"}
        assert kwargs["pd_severity"] == "warning"
        _name, details = mock_route.call_args[0]
        assert details["severity"] == AlertSeverity.HIGH.value

    def test_payload_path_valid(self) -> None:
        ev = _armed(KillSwitchId.KILL_ALL_LIVE)
        with patch("alerting_service.dr_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_kill_switch_armed_payload(ev.model_dump(mode="json"))
        mock_route.assert_called_once()

    def test_payload_path_malformed_dropped(self) -> None:
        with patch("alerting_service.dr_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_kill_switch_armed_payload({"switch_id": "NOT_A_SWITCH"})
        mock_route.assert_not_called()


# ---------------------------------------------------------------------------
# KillSwitchDisarmEvent → recovery AlertCodes
# ---------------------------------------------------------------------------
class TestKillSwitchDisarm:
    def test_auto_cooldown_routes_auto_recovered_code(self) -> None:
        with patch("alerting_service.dr_event_handler.route_event") as mock_route:
            handle_kill_switch_disarm_event(_disarm(BreakerRecoveryMode.AUTO_COOLDOWN, elapsed=420))
        mock_route.assert_called_once()
        event_name, details = mock_route.call_args[0]
        assert event_name == AlertCode.KILL_SWITCH_AUTO_RECOVERED.value
        assert details["severity"] == AlertSeverity.INFO.value
        assert details["recovered_after_seconds"] == 420

    def test_manual_unkill_routes_manual_unkilled_code(self) -> None:
        with patch("alerting_service.dr_event_handler.route_event") as mock_route:
            handle_kill_switch_disarm_event(_disarm(BreakerRecoveryMode.MANUAL_UNKILL, elapsed=None))
        event_name, details = mock_route.call_args[0]
        assert event_name == AlertCode.KILL_SWITCH_MANUAL_UNKILLED.value
        assert details["severity"] == AlertSeverity.INFO.value
        assert details["unkilled_by_operator_id"] == "op-7"

    def test_payload_path_valid(self) -> None:
        ev = _disarm(BreakerRecoveryMode.AUTO_COOLDOWN)
        with patch("alerting_service.dr_event_handler.route_event") as mock_route:
            handle_kill_switch_disarm_payload(ev.model_dump(mode="json"))
        mock_route.assert_called_once()

    def test_payload_path_malformed_dropped(self) -> None:
        with patch("alerting_service.dr_event_handler.route_event") as mock_route:
            handle_kill_switch_disarm_payload({"switch_id": "KILL_ALL_LIVE"})  # missing required fields
        mock_route.assert_not_called()


# ---------------------------------------------------------------------------
# Circuit-breaker fire (classify off BreakerAction)
# ---------------------------------------------------------------------------
def _breaker_with_action(action: BreakerAction) -> CircuitBreakerId:
    for breakers in PER_ARCHETYPE_BREAKERS.values():
        for cfg in breakers:
            if cfg.action is action:
                return cfg.breaker_id
    raise AssertionError(f"no breaker with action={action}")


_ACTION_EXPECTED: dict[BreakerAction, tuple[AlertSeverity, set[str], str | None]] = {
    BreakerAction.KILL_ALL: (AlertSeverity.CRITICAL, {"pagerduty", "telegram"}, "critical"),
    BreakerAction.CANCEL_OPEN: (AlertSeverity.HIGH, {"pagerduty", "telegram"}, "warning"),
    BreakerAction.SCALE_DOWN: (AlertSeverity.WARN, {"telegram"}, None),
    BreakerAction.BLOCK_NEW: (AlertSeverity.WARN, {"telegram"}, None),
}


class TestCircuitBreakerFire:
    @pytest.mark.parametrize("action", list(BreakerAction))
    def test_severity_tier_per_action(self, action: BreakerAction) -> None:
        breaker_id = _breaker_with_action(action)
        expected_sev, expected_channels, expected_pd = _ACTION_EXPECTED[action]
        with patch("alerting_service.dr_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_circuit_breaker_fire(breaker_id, {"applies_to": "CARRY_STAKED_BASIS", "observed": "9.0"})
        mock_route.assert_called_once()
        kwargs = mock_route.call_args.kwargs
        assert kwargs["channels"] == expected_channels
        assert kwargs["pd_severity"] == expected_pd
        _name, details = mock_route.call_args[0]
        assert details["severity"] == expected_sev.value
        assert details["breaker_id"] == breaker_id.value
        assert details["breaker_action"] == action.value
        # original payload fields preserved
        assert details["observed"] == "9.0"

    def test_unknown_breaker_defaults_warn(self) -> None:
        # _breaker_action_for returns None for an id not in the registry → WARN tier.
        # All registry ids ARE present, so simulate by patching the lookup.
        with (
            patch("alerting_service.dr_event_handler._breaker_action_for", return_value=None),
            patch("alerting_service.dr_event_handler.route_event_with_explicit_channels") as mock_route,
        ):
            handle_circuit_breaker_fire(CircuitBreakerId.CLOCK_SKEW_MS, {})
        kwargs = mock_route.call_args.kwargs
        assert kwargs["channels"] == {"telegram"}
        assert kwargs["pd_severity"] is None

    def test_payload_path_unknown_id_dropped(self) -> None:
        with patch("alerting_service.dr_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_circuit_breaker_fire_payload({"breaker_id": "NOPE"})
        mock_route.assert_not_called()

    def test_payload_path_valid(self) -> None:
        with patch("alerting_service.dr_event_handler.route_event_with_explicit_channels") as mock_route:
            handle_circuit_breaker_fire_payload({"breaker_id": CircuitBreakerId.LIQUIDATION_CASCADE_RISK.value})
        mock_route.assert_called_once()

    def test_breaker_action_lookup_resolves_registry_entries(self) -> None:
        # Every registry breaker id resolves to its declared action.
        for breakers in PER_ARCHETYPE_BREAKERS.values():
            for cfg in breakers:
                assert _breaker_action_for(cfg.breaker_id) is cfg.action
