"""Tests for sports alerting rules."""

from __future__ import annotations

from decimal import Decimal

from alerting_service.rules.sports_rules import (
    SportsAlertType,
    check_account_restriction,
    check_arb_alert,
    check_clv_trend,
    check_steam_alert,
)


def test_arb_alert_triggers() -> None:
    alert = check_arb_alert(
        arb_roi_pct=Decimal("5.0"),
        venue_a="betfair_ex_uk",
        venue_b="pinnacle",
        market="soccer_epl_match_1",
    )
    assert alert is not None
    assert alert.alert_type == SportsAlertType.ARB_OPPORTUNITY


def test_steam_alert_triggers() -> None:
    alert = check_steam_alert(
        venue="pinnacle",
        market="soccer_epl_match_1",
        odds_change_pct=Decimal("8.0"),
        time_window_seconds=30,
    )
    assert alert is not None
    assert alert.alert_type == SportsAlertType.STEAM_MOVE


def test_account_restriction_alert() -> None:
    alert = check_account_restriction(
        venue="draftkings",
        restriction_type="stake_limited",
        max_stake_before=Decimal("500"),
        max_stake_after=Decimal("25"),
    )
    assert alert is not None
    assert alert.alert_type == SportsAlertType.ACCOUNT_RESTRICTED


def test_clv_trend_alert() -> None:
    alert = check_clv_trend(
        venue="betfair_ex_uk",
        avg_clv_7d=Decimal("-1.5"),
        avg_clv_30d=Decimal("2.0"),
    )
    assert alert is not None
    assert alert.alert_type == SportsAlertType.CLV_DECLINE
