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
        edge_pct=Decimal("5.0"),
        venue_a="betfair_ex_uk",
        venue_b="pinnacle",
        fixture_id="soccer_epl_match_1",
    )
    assert alert is not None
    assert alert.alert_type == SportsAlertType.ARB_OPPORTUNITY


def test_arb_alert_below_threshold_returns_none() -> None:
    alert = check_arb_alert(
        edge_pct=Decimal("0.5"),
        venue_a="betfair_ex_uk",
        venue_b="pinnacle",
        fixture_id="soccer_epl_match_1",
    )
    assert alert is None


def test_steam_alert_triggers() -> None:
    alert = check_steam_alert(
        sharp_venue="pinnacle",
        movement_pct=Decimal("8.0"),
        stale_venues=["draftkings", "fanduel"],
    )
    assert alert is not None
    assert alert.alert_type == SportsAlertType.STEAM_MOVE


def test_steam_alert_no_stale_venues_returns_none() -> None:
    alert = check_steam_alert(
        sharp_venue="pinnacle",
        movement_pct=Decimal("8.0"),
        stale_venues=[],
    )
    assert alert is None


def test_account_restriction_alert() -> None:
    alert = check_account_restriction(
        venue_key="draftkings",
        previous_max_stake=Decimal("500"),
        current_max_stake=Decimal("25"),
    )
    assert alert is not None
    assert alert.alert_type == SportsAlertType.ACCOUNT_RESTRICTED


def test_account_restriction_no_change_returns_none() -> None:
    alert = check_account_restriction(
        venue_key="draftkings",
        previous_max_stake=Decimal("500"),
        current_max_stake=Decimal("500"),
    )
    assert alert is None


def test_clv_trend_alert() -> None:
    alert = check_clv_trend(
        mean_clv_pct=Decimal("-1.5"),
        sample_size=100,
    )
    assert alert is not None
    assert alert.alert_type == SportsAlertType.CLV_TREND_NEGATIVE


def test_clv_trend_positive_returns_none() -> None:
    alert = check_clv_trend(
        mean_clv_pct=Decimal("2.0"),
        sample_size=100,
    )
    assert alert is None


def test_clv_trend_insufficient_sample_returns_none() -> None:
    alert = check_clv_trend(
        mean_clv_pct=Decimal("-1.5"),
        sample_size=10,
    )
    assert alert is None
