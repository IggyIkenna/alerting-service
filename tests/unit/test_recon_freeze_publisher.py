"""Synthetic unit tests for the recon-freeze coordination-event publisher.

Codex name: "assert recon-freeze armed within 5s of SEV0 fire" — the synthetic
version asserts that the publish actually happens (and with the correct scope)
the moment a CRITICAL recon-age breach or a SEV0 override fires.

Covers (observability_master G12 P0):
  1. A CRITICAL recon-age payload publishes exactly one RECON_FREEZE_ARMED with
     scope="symbol" + correct strategy/venue/instrument.
  2. A WARNING recon-age payload publishes NOTHING.
  3. Each of the 7 sev0 overrides publishes RECON_FREEZE_ARMED with the correct
     scope (account vs symbol).
  4. publish_recon_freeze_lifted emits RECON_FREEZE_LIFTED.

Pure / credential-free — ``publish_coordination_event`` is patched where it is
imported into ``recon_freeze_publisher`` so no real bus publish occurs.
"""

from __future__ import annotations

from unittest.mock import patch

from unified_api_contracts.incident import ImmediateSev0Override

from alerting_service.recon_freeze_publisher import (
    _ACCOUNT_WIDE_OVERRIDES,
    ACCOUNT_WIDE_INSTRUMENT,
    RECON_FREEZE_ARMED,
    RECON_FREEZE_LIFTED,
    SCOPE_ACCOUNT,
    SCOPE_SYMBOL,
    arm_recon_freeze_for_alerts,
    publish_recon_freeze_armed,
    publish_recon_freeze_lifted,
)
from alerting_service.rules.reconciliation_rules import (
    _SEV0_PREDICATE_KEYS,
    evaluate_immediate_sev0,
    evaluate_recon_age,
)

_PUBLISH = "alerting_service.recon_freeze_publisher.publish_coordination_event"


class TestCriticalReconAgeArmsSymbolScope:
    def test_critical_recon_age_publishes_one_symbol_freeze(self) -> None:
        # 2000s unreconciled > critical_seconds (1800) → CRITICAL recon-age.
        payload: dict[str, object] = {
            "dimension": "POSITION",
            "venue": "binance",
            "instrument": "BTC-USDT",
            "strategy_id": "carry_staked_basis",
            "unreconciled_age_seconds": "2000",
        }
        recon_alerts = evaluate_recon_age(payload)
        assert len(recon_alerts) == 1
        assert recon_alerts[0]["rule_id"] == "RECONCILIATION_AGE_CRITICAL"

        with patch(_PUBLISH) as mock_publish:
            published = arm_recon_freeze_for_alerts(recon_alerts, [])

        assert len(published) == 1
        assert mock_publish.call_count == 1
        (event_type,), kwargs = mock_publish.call_args
        assert event_type == RECON_FREEZE_ARMED
        emitted = kwargs["payload"]
        assert emitted["scope"] == SCOPE_SYMBOL
        assert emitted["strategy_id"] == "carry_staked_basis"
        assert emitted["venue"] == "binance"
        assert emitted["instrument"] == "BTC-USDT"
        assert "RECONCILIATION_AGE_CRITICAL" in str(emitted["reason"])


class TestWarningReconAgePublishesNothing:
    def test_warning_recon_age_arms_no_freeze(self) -> None:
        # 400s: in [warn=300, investigate=900) → WARNING band, not CRITICAL.
        payload: dict[str, object] = {
            "dimension": "POSITION",
            "venue": "bybit",
            "instrument": "ETH-USDT",
            "strategy_id": "s1",
            "unreconciled_age_seconds": "400",
        }
        recon_alerts = evaluate_recon_age(payload)
        assert len(recon_alerts) == 1
        assert recon_alerts[0]["rule_id"] == "RECONCILIATION_AGE_WARN"

        with patch(_PUBLISH) as mock_publish:
            published = arm_recon_freeze_for_alerts(recon_alerts, [])

        assert published == []
        mock_publish.assert_not_called()

    def test_below_warn_band_emits_no_alert_and_no_freeze(self) -> None:
        payload: dict[str, object] = {
            "venue": "okx",
            "instrument": "SOL-USDT",
            "strategy_id": "s2",
            "unreconciled_age_seconds": "10",
        }
        recon_alerts = evaluate_recon_age(payload)
        assert recon_alerts == []
        with patch(_PUBLISH) as mock_publish:
            published = arm_recon_freeze_for_alerts(recon_alerts, [])
        assert published == []
        mock_publish.assert_not_called()


class TestEachSev0OverridePublishesCorrectScope:
    def test_all_seven_overrides_publish_with_correct_scope(self) -> None:
        for override, key in _SEV0_PREDICATE_KEYS.items():
            row: dict[str, object] = {
                "venue": "binance",
                "instrument": "BTC-USDT",
                "strategy_id": "carry_staked_basis",
                "account_id": "acct-1",
                "client_id": "client-1",
                key: True,
            }
            sev0_alerts = evaluate_immediate_sev0(row)
            assert len(sev0_alerts) == 1, f"{override} should fire exactly one alert"
            assert sev0_alerts[0]["rule_id"] == f"IMMEDIATE_SEV0_{override.value}"

            with patch(_PUBLISH) as mock_publish:
                published = arm_recon_freeze_for_alerts([], sev0_alerts)

            assert len(published) == 1
            assert mock_publish.call_count == 1
            (event_type,), kwargs = mock_publish.call_args
            assert event_type == RECON_FREEZE_ARMED
            emitted = kwargs["payload"]

            expected_scope = SCOPE_ACCOUNT if override in _ACCOUNT_WIDE_OVERRIDES else SCOPE_SYMBOL
            assert emitted["scope"] == expected_scope, f"{override} → expected scope {expected_scope}"

            if expected_scope == SCOPE_ACCOUNT:
                assert emitted["instrument"] == ACCOUNT_WIDE_INSTRUMENT
                assert emitted["account_id"] == "acct-1"
                assert emitted["client_id"] == "client-1"
            else:
                assert emitted["instrument"] == "BTC-USDT"
            assert emitted["strategy_id"] == "carry_staked_basis"
            assert override.value in str(emitted["reason"])

    def test_account_wide_set_is_the_four_account_level_overrides(self) -> None:
        # Lock the operator's account-vs-symbol classification.
        assert (
            frozenset(
                {
                    ImmediateSev0Override.UNKNOWN_NET_EXPOSURE,
                    ImmediateSev0Override.MATERIAL_BALANCE_MOVEMENT_UNEXPLAINED,
                    ImmediateSev0Override.MARGIN_COLLATERAL_SAFETY_UNCERTAIN,
                    ImmediateSev0Override.KILL_SWITCH_CANNOT_CONFIRM_CANCEL,
                }
            )
            == _ACCOUNT_WIDE_OVERRIDES
        )
        # The remaining three are symbol-scoped.
        symbol_scoped = set(ImmediateSev0Override) - _ACCOUNT_WIDE_OVERRIDES
        assert symbol_scoped == {
            ImmediateSev0Override.OPEN_ORDERS_UNCONFIRMABLE,
            ImmediateSev0Override.VENUE_INTERNAL_BALANCE_MISMATCH,
            ImmediateSev0Override.POSITION_EXISTS_EXTERNALLY_UNKNOWN_INTERNALLY,
        }


class TestUnknownSev0RuleIdSkipped:
    def test_unknown_rule_id_does_not_publish(self) -> None:
        bogus = [{"rule_id": "IMMEDIATE_SEV0_NOT_A_REAL_OVERRIDE", "severity": "CRITICAL"}]
        with patch(_PUBLISH) as mock_publish:
            published = arm_recon_freeze_for_alerts([], bogus)
        assert published == []
        mock_publish.assert_not_called()


class TestPublishLifted:
    def test_publish_recon_freeze_lifted_emits_lifted_event(self) -> None:
        with patch(_PUBLISH) as mock_publish:
            publish_recon_freeze_lifted(
                strategy_id="carry_staked_basis",
                venue="binance",
                instrument="BTC-USDT",
                scope=SCOPE_SYMBOL,
                reason="operator unfreeze ticket-123",
            )
        assert mock_publish.call_count == 1
        (event_type,), kwargs = mock_publish.call_args
        assert event_type == RECON_FREEZE_LIFTED
        emitted = kwargs["payload"]
        assert emitted["scope"] == SCOPE_SYMBOL
        assert emitted["strategy_id"] == "carry_staked_basis"
        assert emitted["venue"] == "binance"
        assert emitted["instrument"] == "BTC-USDT"
        assert emitted["reason"] == "operator unfreeze ticket-123"

    def test_publish_armed_direct_account_wide(self) -> None:
        with patch(_PUBLISH) as mock_publish:
            publish_recon_freeze_armed(
                strategy_id="s9",
                venue="okx",
                instrument=ACCOUNT_WIDE_INSTRUMENT,
                scope=SCOPE_ACCOUNT,
                reason="UNKNOWN_NET_EXPOSURE",
                account_id="acct-9",
                client_id="client-9",
            )
        (event_type,), kwargs = mock_publish.call_args
        assert event_type == RECON_FREEZE_ARMED
        emitted = kwargs["payload"]
        assert emitted["scope"] == SCOPE_ACCOUNT
        assert emitted["instrument"] == ACCOUNT_WIDE_INSTRUMENT
        assert emitted["account_id"] == "acct-9"
