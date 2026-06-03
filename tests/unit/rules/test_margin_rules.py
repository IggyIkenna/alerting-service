"""Unit tests for margin_rules MarginEvent consumer (Wave C4)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from unified_api_contracts.internal import (
    MarginEvent,
    MarginEventSeverity,
    MarginHealthSnapshot,
)

from alerting_service.rules.margin_rules import (
    route_margin_event,
    route_margin_event_payload,
)


def _make_snapshot(**overrides: object) -> MarginHealthSnapshot:
    base: dict[str, object] = {
        "strategy_id": "carry_recursive_staked_eth",
        "timestamp": datetime.now(UTC),
        "venue": "aave_v3",
        "venue_type": "defi",
        "position_type": "DEBT_TOKEN",
        "health_factor": Decimal("1.10"),
        "ltv_ratio": Decimal("0.78"),
        "collateral_usd": Decimal("100000"),
        "debt_usd": Decimal("75000"),
    }
    base.update(overrides)
    return MarginHealthSnapshot.model_validate(base)


def _make_event(
    severity: MarginEventSeverity = MarginEventSeverity.CRITICAL,
    snapshot: MarginHealthSnapshot | None = None,
    breached: str = "health_factor<critical",
) -> MarginEvent:
    return MarginEvent(
        event_id=str(uuid.uuid4()),
        run_id="run_test",
        scenario_id=None,
        correlation_id=str(uuid.uuid4()),
        causation_id=None,
        created_at=datetime.now(UTC),
        margin_severity=severity,
        snapshot=snapshot or _make_snapshot(),
        threshold_breached=breached,
        distance_to_liquidation_pct=Decimal("4.5"),
        recommended_action="reduce position 25%",
    )


class TestSeverityToEventName:
    def test_warning_severity_maps_to_margin_warning(self) -> None:
        event = _make_event(severity=MarginEventSeverity.WARNING)
        with patch("alerting_service.rules.margin_rules.route_event") as mock_route:
            route_margin_event(event)
        mock_route.assert_called_once()
        event_name, _ = mock_route.call_args.args
        assert event_name == "MARGIN_WARNING"

    def test_critical_severity_maps_to_margin_critical(self) -> None:
        event = _make_event(severity=MarginEventSeverity.CRITICAL)
        with patch("alerting_service.rules.margin_rules.route_event") as mock_route:
            route_margin_event(event)
        event_name, _ = mock_route.call_args.args
        assert event_name == "MARGIN_CRITICAL"

    def test_liquidation_severity_maps_to_margin_liquidation(self) -> None:
        event = _make_event(severity=MarginEventSeverity.LIQUIDATION)
        with patch("alerting_service.rules.margin_rules.route_event") as mock_route:
            route_margin_event(event)
        event_name, _ = mock_route.call_args.args
        assert event_name == "MARGIN_LIQUIDATION"

    def test_info_severity_maps_to_margin_info(self) -> None:
        event = _make_event(severity=MarginEventSeverity.INFO)
        with patch("alerting_service.rules.margin_rules.route_event") as mock_route:
            route_margin_event(event)
        event_name, _ = mock_route.call_args.args
        assert event_name == "MARGIN_INFO"


class TestPayloadShape:
    def test_payload_carries_snapshot_and_envelope_fields(self) -> None:
        event = _make_event()
        with patch("alerting_service.rules.margin_rules.route_event") as mock_route:
            route_margin_event(event)
        _, payload = mock_route.call_args.args
        assert payload["strategy_id"] == "carry_recursive_staked_eth"
        assert payload["venue"] == "aave_v3"
        assert payload["venue_type"] == "defi"
        assert payload["health_factor"] == 1.10
        assert payload["ltv_ratio"] == 0.78
        assert payload["distance_to_liquidation_pct"] == 4.5
        assert payload["recommended_action"] == "reduce position 25%"
        assert payload["threshold_breached"] == "health_factor<critical"
        assert payload["correlation_id"] == event.correlation_id
        assert payload["source"] == "alerting-service/margin-rules"

    def test_payload_omits_unset_optional_fields(self) -> None:
        # Snapshot with only the strictly required fields and no margin metrics.
        snap = MarginHealthSnapshot(
            strategy_id="strat_x",
            timestamp=datetime.now(UTC),
            venue="binance",
            venue_type="cefi",
            position_type="PERPETUAL",
        )
        event = _make_event(snapshot=snap)
        # Drop the optional distance_to_liquidation_pct and recommended_action.
        event = event.model_copy(update={"distance_to_liquidation_pct": None, "recommended_action": None})
        with patch("alerting_service.rules.margin_rules.route_event") as mock_route:
            route_margin_event(event)
        _, payload = mock_route.call_args.args
        assert "health_factor" not in payload
        assert "ltv_ratio" not in payload
        assert "margin_usage_pct" not in payload
        assert "distance_to_liquidation_pct" not in payload
        assert "recommended_action" not in payload


class TestPayloadDeserialization:
    def test_valid_payload_routes_through(self) -> None:
        event = _make_event()
        payload = event.model_dump(mode="json")
        with patch("alerting_service.rules.margin_rules.route_event") as mock_route:
            route_margin_event_payload(payload)
        mock_route.assert_called_once()
        event_name, _ = mock_route.call_args.args
        assert event_name == "MARGIN_CRITICAL"

    def test_malformed_payload_drops_silently(self) -> None:
        # Missing required fields -> validation fails -> dropped, not raised.
        with patch("alerting_service.rules.margin_rules.route_event") as mock_route:
            route_margin_event_payload({"event_type": "MarginEvent", "broken": True})
        mock_route.assert_not_called()


class TestMessageFormat:
    def test_message_contains_diagnostic_context(self) -> None:
        event = _make_event()
        with patch("alerting_service.rules.margin_rules.route_event") as mock_route:
            route_margin_event(event)
        _, payload = mock_route.call_args.args
        message = str(payload["message"])
        assert "venue=aave_v3" in message
        assert "HF=1.1" in message
        assert "breached=health_factor<critical" in message
