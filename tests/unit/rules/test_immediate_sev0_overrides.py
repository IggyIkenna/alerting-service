"""Unit tests for evaluate_immediate_sev0 — one test per ImmediateSev0Override predicate.

Pure-function tests, no mocks, credential-free.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from unified_api_contracts.incident import ImmediateSev0Override

from alerting_service.rules.reconciliation_rules import evaluate_immediate_sev0


def _base_row(**overrides: object) -> dict[str, object]:
    return {
        "venue": "binance",
        "instrument": "BTC-USDT",
        "strategy_id": "DEFI_carry_staked_basis_v1",
        **overrides,
    }


def _assert_sev0_alert(
    alert: dict[str, object],
    expected_override: ImmediateSev0Override,
) -> None:
    assert alert["severity"] == "CRITICAL"
    assert alert["delivery_channel"] == "pagerduty+telegram"
    assert alert["delivered"] is False
    assert alert["override"] == expected_override.value
    assert expected_override.value in str(alert["rule_id"])
    assert datetime.fromisoformat(str(alert["created_at"]))


class TestUnknownNetExposure:
    def test_triggers_when_true(self) -> None:
        row = _base_row(unknown_net_exposure=True)
        alerts = evaluate_immediate_sev0(row)
        assert len(alerts) == 1
        _assert_sev0_alert(alerts[0], ImmediateSev0Override.UNKNOWN_NET_EXPOSURE)

    def test_no_alert_when_false(self) -> None:
        row = _base_row(unknown_net_exposure=False)
        assert evaluate_immediate_sev0(row) == []

    def test_no_alert_when_absent(self) -> None:
        assert evaluate_immediate_sev0(_base_row()) == []


class TestOpenOrdersUnconfirmable:
    def test_triggers_when_true(self) -> None:
        row = _base_row(open_orders_unconfirmable=True)
        alerts = evaluate_immediate_sev0(row)
        assert len(alerts) == 1
        _assert_sev0_alert(alerts[0], ImmediateSev0Override.OPEN_ORDERS_UNCONFIRMABLE)

    def test_no_alert_when_false(self) -> None:
        assert evaluate_immediate_sev0(_base_row(open_orders_unconfirmable=False)) == []


class TestKillSwitchCannotConfirmCancel:
    def test_triggers_when_true(self) -> None:
        row = _base_row(kill_switch_cannot_confirm_cancel=True)
        alerts = evaluate_immediate_sev0(row)
        assert len(alerts) == 1
        _assert_sev0_alert(alerts[0], ImmediateSev0Override.KILL_SWITCH_CANNOT_CONFIRM_CANCEL)

    def test_no_alert_when_false(self) -> None:
        assert evaluate_immediate_sev0(_base_row(kill_switch_cannot_confirm_cancel=False)) == []


class TestVenueInternalBalanceMismatch:
    def test_triggers_when_true(self) -> None:
        row = _base_row(venue_internal_balance_mismatch=True)
        alerts = evaluate_immediate_sev0(row)
        assert len(alerts) == 1
        _assert_sev0_alert(alerts[0], ImmediateSev0Override.VENUE_INTERNAL_BALANCE_MISMATCH)

    def test_no_alert_when_false(self) -> None:
        assert evaluate_immediate_sev0(_base_row(venue_internal_balance_mismatch=False)) == []


class TestPositionExistsExternallyUnknownInternally:
    def test_triggers_when_true(self) -> None:
        row = _base_row(position_exists_externally_unknown_internally=True)
        alerts = evaluate_immediate_sev0(row)
        assert len(alerts) == 1
        _assert_sev0_alert(
            alerts[0],
            ImmediateSev0Override.POSITION_EXISTS_EXTERNALLY_UNKNOWN_INTERNALLY,
        )

    def test_no_alert_when_false(self) -> None:
        assert evaluate_immediate_sev0(_base_row(position_exists_externally_unknown_internally=False)) == []


class TestMaterialBalanceMovementUnexplained:
    def test_triggers_when_true(self) -> None:
        row = _base_row(material_balance_movement_unexplained=True)
        alerts = evaluate_immediate_sev0(row)
        assert len(alerts) == 1
        _assert_sev0_alert(alerts[0], ImmediateSev0Override.MATERIAL_BALANCE_MOVEMENT_UNEXPLAINED)

    def test_no_alert_when_false(self) -> None:
        assert evaluate_immediate_sev0(_base_row(material_balance_movement_unexplained=False)) == []


class TestMarginCollateralSafetyUncertain:
    def test_triggers_when_true(self) -> None:
        row = _base_row(margin_collateral_safety_uncertain=True)
        alerts = evaluate_immediate_sev0(row)
        assert len(alerts) == 1
        _assert_sev0_alert(alerts[0], ImmediateSev0Override.MARGIN_COLLATERAL_SAFETY_UNCERTAIN)

    def test_no_alert_when_false(self) -> None:
        assert evaluate_immediate_sev0(_base_row(margin_collateral_safety_uncertain=False)) == []


class TestMultiplePredicatesSimultaneous:
    def test_two_triggers_produce_two_alerts(self) -> None:
        row = _base_row(
            unknown_net_exposure=True,
            margin_collateral_safety_uncertain=True,
        )
        alerts = evaluate_immediate_sev0(row)
        assert len(alerts) == 2
        overrides_fired = {str(a["override"]) for a in alerts}
        assert ImmediateSev0Override.UNKNOWN_NET_EXPOSURE.value in overrides_fired
        assert ImmediateSev0Override.MARGIN_COLLATERAL_SAFETY_UNCERTAIN.value in overrides_fired

    def test_all_false_returns_empty(self) -> None:
        row = _base_row(
            unknown_net_exposure=False,
            open_orders_unconfirmable=False,
            kill_switch_cannot_confirm_cancel=False,
            venue_internal_balance_mismatch=False,
            position_exists_externally_unknown_internally=False,
            material_balance_movement_unexplained=False,
            margin_collateral_safety_uncertain=False,
        )
        assert evaluate_immediate_sev0(row) == []

    def test_venue_and_strategy_propagated_to_alerts(self) -> None:
        row = _base_row(unknown_net_exposure=True)
        alert = evaluate_immediate_sev0(row)[0]
        assert alert["venue"] == "binance"
        assert alert["strategy_id"] == "DEFI_carry_staked_basis_v1"


@pytest.mark.parametrize(
    "predicate_key",
    [p.value.lower() for p in ImmediateSev0Override],
)
def test_closed_set_coverage(predicate_key: str) -> None:
    """Every ImmediateSev0Override member is exercised by a non-empty row."""
    row = _base_row(**{predicate_key.replace(".", "_"): True})
    # Accept snake_case — remove dots and normalise
    snake = predicate_key.replace("-", "_")
    row = _base_row(**{snake: True})
    alerts = evaluate_immediate_sev0(row)
    assert len(alerts) >= 1, f"predicate {predicate_key!r} produced no alert"
