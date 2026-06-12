"""Unit tests for alerting_service.cefi_ml_event_handler.

Verifies the 3-tier ML_SIGNAL_STALENESS ladder and pass-through routing:

  age < 4h   → no alert
  4h ≤ age < 12h  → route_event("ML_SIGNAL_STALENESS") WARN
  12h ≤ age < 24h → route_event_with_explicit_channels CRITICAL
  age ≥ 24h       → route_event("KILL_SWITCH_ML_MODEL_FAILURE")

Pass-through events (ML_PNL_DEVIATION etc.) go to generic route_event.
"""

from __future__ import annotations

from unittest.mock import call, patch

import pytest

from alerting_service.cefi_ml_event_handler import (
    CEFI_ML_EVENT_NAMES,
    ML_SIGNAL_STALENESS_EVENT,
    handle_cefi_ml_event,
)

_ROUTE_EVENT = "alerting_service.cefi_ml_event_handler.route_event"
_ROUTE_EXPLICIT = "alerting_service.cefi_ml_event_handler.route_event_with_explicit_channels"

_GOOD_PAYLOAD: dict[str, object] = {
    "archetype": "ML_DIRECTIONAL_CONTINUOUS",
    "venue": "OKX",
    "model_version": "v3.2.1",
}


@pytest.mark.unit
class TestMlSignalStalenessNoAlert:
    def test_below_warn_threshold_no_alert(self) -> None:
        """age < 4h → no alert routed."""
        payload = {**_GOOD_PAYLOAD, "age_seconds": 3 * 3600}
        with patch(_ROUTE_EVENT) as mock_route, patch(_ROUTE_EXPLICIT) as mock_explicit:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, payload)
        assert mock_route.call_count == 0
        assert mock_explicit.call_count == 0

    def test_exactly_zero_age_no_alert(self) -> None:
        """age = 0 → no alert."""
        payload = {**_GOOD_PAYLOAD, "age_seconds": 0.0}
        with patch(_ROUTE_EVENT) as mock_route:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, payload)
        assert mock_route.call_count == 0

    def test_missing_age_seconds_safe_skip(self) -> None:
        """Missing age_seconds → no crash, no alert."""
        with patch(_ROUTE_EVENT) as mock_route, patch(_ROUTE_EXPLICIT) as mock_explicit:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, {"archetype": "ML_DIRECTIONAL_CONTINUOUS"})
        assert mock_route.call_count == 0
        assert mock_explicit.call_count == 0

    def test_invalid_age_seconds_safe_skip(self) -> None:
        """Non-numeric age_seconds → no crash, no alert."""
        with patch(_ROUTE_EVENT) as mock_route:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, {**_GOOD_PAYLOAD, "age_seconds": "not-a-number"})
        assert mock_route.call_count == 0


@pytest.mark.unit
class TestMlSignalStalenessWarn:
    def test_exactly_at_warn_threshold(self) -> None:
        """age = 4h exactly → WARN route_event(ML_SIGNAL_STALENESS)."""
        payload = {**_GOOD_PAYLOAD, "age_seconds": 4 * 3600}
        with patch(_ROUTE_EVENT) as mock_route, patch(_ROUTE_EXPLICIT) as mock_explicit:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, payload)
        assert mock_route.call_count == 1
        assert mock_explicit.call_count == 0
        event_name = mock_route.call_args.args[0]
        assert event_name == "ML_SIGNAL_STALENESS"
        details = mock_route.call_args.args[1]
        assert details["severity"] == "warning"

    def test_just_below_critical_threshold(self) -> None:
        """age = 11.9h → WARN (below 12h critical threshold)."""
        payload = {**_GOOD_PAYLOAD, "age_seconds": 11.9 * 3600}
        with patch(_ROUTE_EVENT) as mock_route, patch(_ROUTE_EXPLICIT) as mock_explicit:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, payload)
        assert mock_route.call_count == 1
        assert mock_explicit.call_count == 0
        assert mock_route.call_args.args[1]["severity"] == "warning"

    def test_warn_details_contain_archetype_and_venue(self) -> None:
        """WARN details include archetype and venue from payload."""
        payload = {**_GOOD_PAYLOAD, "age_seconds": 5 * 3600}
        with patch(_ROUTE_EVENT) as mock_route:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, payload)
        details = mock_route.call_args.args[1]
        assert details["archetype"] == "ML_DIRECTIONAL_CONTINUOUS"
        assert details["venue"] == "OKX"
        assert details["model_version"] == "v3.2.1"


@pytest.mark.unit
class TestMlSignalStalenessCritical:
    def test_exactly_at_critical_threshold(self) -> None:
        """age = 12h exactly → CRITICAL route_event_with_explicit_channels."""
        payload = {**_GOOD_PAYLOAD, "age_seconds": 12 * 3600}
        with patch(_ROUTE_EVENT) as mock_route, patch(_ROUTE_EXPLICIT) as mock_explicit:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, payload)
        assert mock_route.call_count == 0
        assert mock_explicit.call_count == 1
        event_name = mock_explicit.call_args.args[0]
        assert event_name == "ML_SIGNAL_STALENESS"
        details = mock_explicit.call_args.args[1]
        assert details["severity"] == "critical"

    def test_critical_pages_pagerduty_and_telegram(self) -> None:
        """CRITICAL tier routes to pagerduty + telegram."""
        payload = {**_GOOD_PAYLOAD, "age_seconds": 18 * 3600}
        with patch(_ROUTE_EVENT), patch(_ROUTE_EXPLICIT) as mock_explicit:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, payload)
        kwargs = mock_explicit.call_args.kwargs
        assert "pagerduty" in kwargs["channels"]
        assert "telegram" in kwargs["channels"]

    def test_just_below_kill_threshold(self) -> None:
        """age = 23.9h → still CRITICAL, not kill-switch."""
        payload = {**_GOOD_PAYLOAD, "age_seconds": 23.9 * 3600}
        with patch(_ROUTE_EVENT) as mock_route, patch(_ROUTE_EXPLICIT) as mock_explicit:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, payload)
        assert mock_route.call_count == 0
        assert mock_explicit.call_count == 1


@pytest.mark.unit
class TestMlSignalStalenessKillSwitch:
    def test_exactly_at_kill_threshold(self) -> None:
        """age = 24h → KILL_SWITCH_ML_MODEL_FAILURE via route_event."""
        payload = {**_GOOD_PAYLOAD, "age_seconds": 24 * 3600}
        with patch(_ROUTE_EVENT) as mock_route, patch(_ROUTE_EXPLICIT) as mock_explicit:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, payload)
        assert mock_route.call_count == 1
        assert mock_explicit.call_count == 0
        event_name = mock_route.call_args.args[0]
        assert event_name == "KILL_SWITCH_ML_MODEL_FAILURE"

    def test_beyond_kill_threshold(self) -> None:
        """age = 48h → KILL_SWITCH_ML_MODEL_FAILURE (not CRITICAL)."""
        payload = {**_GOOD_PAYLOAD, "age_seconds": 48 * 3600}
        with patch(_ROUTE_EVENT) as mock_route, patch(_ROUTE_EXPLICIT) as mock_explicit:
            handle_cefi_ml_event(ML_SIGNAL_STALENESS_EVENT, payload)
        assert mock_route.call_count == 1
        assert mock_explicit.call_count == 0
        assert mock_route.call_args.args[0] == "KILL_SWITCH_ML_MODEL_FAILURE"


@pytest.mark.unit
class TestCefiMlPassthrough:
    def test_pnl_deviation_routes_via_generic(self) -> None:
        """ML_PNL_DEVIATION passthrough → route_event called once."""
        with patch(_ROUTE_EVENT) as mock_route:
            handle_cefi_ml_event("ML_PNL_DEVIATION", {"deviation_bps": 300.0})
        assert mock_route.call_count == 1
        assert mock_route.call_args.args[0] == "ML_PNL_DEVIATION"

    def test_order_rejection_spike_routes_via_generic(self) -> None:
        """ORDER_REJECTION_SPIKE passthrough → route_event called once."""
        with patch(_ROUTE_EVENT) as mock_route:
            handle_cefi_ml_event("ORDER_REJECTION_SPIKE", {"rejection_rate": 0.25})
        assert mock_route.call_count == 1

    def test_position_discrepancy_routes_via_generic(self) -> None:
        """POSITION_CRITICAL_DISCREPANCY passthrough → route_event called once."""
        with patch(_ROUTE_EVENT) as mock_route:
            handle_cefi_ml_event("POSITION_CRITICAL_DISCREPANCY", {"discrepancy_usd": 5000.0})
        assert mock_route.call_count == 1

    def test_unknown_event_drops_silently(self) -> None:
        """Event name not in CEFI_ML_EVENT_NAMES → no crash, no route."""
        with patch(_ROUTE_EVENT) as mock_route:
            handle_cefi_ml_event("SOME_UNKNOWN_ML_EVENT", {})
        assert mock_route.call_count == 0


@pytest.mark.unit
class TestCefiMlEventNames:
    def test_ml_signal_staleness_in_event_names(self) -> None:
        assert "ML_SIGNAL_STALENESS" in CEFI_ML_EVENT_NAMES

    def test_passthrough_names_in_event_names(self) -> None:
        assert "ML_PNL_DEVIATION" in CEFI_ML_EVENT_NAMES
        assert "ORDER_REJECTION_SPIKE" in CEFI_ML_EVENT_NAMES
        assert "POSITION_CRITICAL_DISCREPANCY" in CEFI_ML_EVENT_NAMES
