"""Unit tests for P0.12-P0.14 router incident-envelope extensions.

P0.12 — route_legacy_alert() backward-compat shim.
P0.13 — AUTO_ACTION_SUCCEEDED recovery-verification gate.
P0.14 — ImmediateSev0Override pre-evaluation → SEV0 dispatch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from unified_api_contracts.alerting import AlertSeverity
from unified_api_contracts.incident import (
    ImmediateSev0Override,
    IncidentEnvelope,
    IncidentState,
    RecoveryVerificationResult,
)

from alerting_service.notifiers.router import (
    _escalate_severity,
    _extract_sev0_overrides,
    _handle_auto_action_recovery,
    route_incident,
    route_legacy_alert,
)

_NOW = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
_INCIDENT_KEY = "test-inc-router-001"


def _envelope(**overrides: object) -> IncidentEnvelope:
    defaults: dict[str, object] = {
        "event_id": "evt-router-001",
        "incident_key": _INCIDENT_KEY,
        "timestamp": _NOW,
        "state": IncidentState.DETECTED,
        "environment": "prod",
        "severity_hint": AlertSeverity.HIGH,
        "domain": "live_trading",
        "service": "execution-service",
        "component": "order-router",
        "problem_type": "OOM_DETECTED",
        "problem_summary": "OOM kill",
        "config_hash": "sha256:abc",
        "code_version": "v1.0.0",
        "human_audit_ack_required": False,
        "audit_ack_due_at": _NOW + timedelta(hours=6),
    }
    defaults.update(overrides)
    return IncidentEnvelope(**defaults)  # type: ignore[arg-type]


# ── P0.14 — ImmediateSev0Override helpers ─────────────────────────────────


class TestExtractSev0Overrides:
    def test_returns_tuple_for_known_override(self):
        env = _envelope(problem_type=ImmediateSev0Override.UNKNOWN_NET_EXPOSURE.value)
        assert _extract_sev0_overrides(env) == ("UNKNOWN_NET_EXPOSURE",)

    def test_returns_empty_for_non_override(self):
        env = _envelope(problem_type="OOM_DETECTED")
        assert _extract_sev0_overrides(env) == ()

    @pytest.mark.parametrize("override", list(ImmediateSev0Override))
    def test_all_7_overrides_detected(self, override: ImmediateSev0Override):
        env = _envelope(problem_type=override.value)
        result = _extract_sev0_overrides(env)
        assert len(result) == 1
        assert result[0] == override.value


class TestEscalateSeverity:
    def test_info_to_warn(self):
        assert _escalate_severity(AlertSeverity.INFO) == AlertSeverity.WARN

    def test_warn_to_high(self):
        assert _escalate_severity(AlertSeverity.WARN) == AlertSeverity.HIGH

    def test_high_to_critical(self):
        assert _escalate_severity(AlertSeverity.HIGH) == AlertSeverity.CRITICAL

    def test_critical_stays_critical(self):
        assert _escalate_severity(AlertSeverity.CRITICAL) == AlertSeverity.CRITICAL


# ── P0.14 — route_incident() SEV0 dispatch ────────────────────────────────


class TestRouteIncidentSev0Override:
    def test_sev0_override_calls_fallback(self):
        env = _envelope(problem_type=ImmediateSev0Override.OPEN_ORDERS_UNCONFIRMABLE.value)
        with (
            patch("alerting_service.notifiers.router._dispatch_sev0_fallbacks") as mock_fallback,
            patch("alerting_service.notifiers.router._route_envelope_to_channels"),
        ):
            route_incident(env)
            mock_fallback.assert_called_once_with(env, ("OPEN_ORDERS_UNCONFIRMABLE",))

    def test_non_sev0_does_not_call_fallback(self):
        env = _envelope(problem_type="OOM_DETECTED")
        with (
            patch("alerting_service.notifiers.router._dispatch_sev0_fallbacks") as mock_fallback,
            patch("alerting_service.notifiers.router._route_envelope_to_channels"),
        ):
            route_incident(env)
            mock_fallback.assert_not_called()

    def test_sev0_still_routes_to_channels(self):
        env = _envelope(problem_type=ImmediateSev0Override.VENUE_INTERNAL_BALANCE_MISMATCH.value)
        with (
            patch("alerting_service.notifiers.router._dispatch_sev0_fallbacks"),
            patch("alerting_service.notifiers.router._route_envelope_to_channels") as mock_route,
        ):
            route_incident(env)
            mock_route.assert_called_once_with(env)


# ── P0.13 — AUTO_ACTION_SUCCEEDED recovery gate ───────────────────────────


def _make_sm_mock(
    *,
    verif_succeeds: bool = True,
    confirmed_succeeds: bool = True,
    uncertain_succeeds: bool = True,
) -> MagicMock:
    """Build an IncidentStateMachine mock with configurable transition outcomes."""
    sm = MagicMock()

    def _transition(envelope: IncidentEnvelope, target: IncidentState, **kwargs: object):
        result = MagicMock()
        result.envelope = envelope.model_copy(update={"state": target, **dict(kwargs.items())})
        if target is IncidentState.RECOVERY_VERIFICATION_STARTED:
            result.succeeded = verif_succeeds
        elif target is IncidentState.RECOVERY_CONFIRMED:
            result.succeeded = confirmed_succeeds
        elif target is IncidentState.RECOVERY_UNCERTAIN:
            result.succeeded = uncertain_succeeds
        else:
            result.succeeded = True
        result.failure_reason = None if result.succeeded else "blocked"
        return result

    sm.transition.side_effect = _transition
    return sm


def _make_all_passed_verifier() -> MagicMock:
    verifier = MagicMock()
    verifier.verify.return_value = RecoveryVerificationResult(
        health_checks_passed=True,
        positions_reconciled=True,
        orders_reconciled=True,
        market_data_fresh=True,
        strategy_state_restored=True,
    )
    return verifier


def _make_failing_verifier() -> MagicMock:
    verifier = MagicMock()
    verifier.verify.return_value = RecoveryVerificationResult(
        health_checks_passed=False,
        positions_reconciled=True,
        orders_reconciled=True,
        market_data_fresh=True,
        strategy_state_restored=True,
        failure_reasons=("health check failed",),
    )
    return verifier


class TestHandleAutoActionRecovery:
    def test_recovery_confirmed_routes_confirmed_envelope(self):
        env = _envelope(state=IncidentState.AUTO_ACTION_SUCCEEDED)
        sm = _make_sm_mock()
        verifier = _make_all_passed_verifier()
        with patch("alerting_service.notifiers.router._route_envelope_to_channels") as mock_route:
            _handle_auto_action_recovery(env, _sm=sm, _verifier=verifier)
            routed_state = mock_route.call_args[0][0].state
            assert routed_state is IncidentState.RECOVERY_CONFIRMED

    def test_recovery_uncertain_routes_uncertain_envelope(self):
        env = _envelope(state=IncidentState.AUTO_ACTION_SUCCEEDED)
        sm = _make_sm_mock()
        verifier = _make_failing_verifier()
        with patch("alerting_service.notifiers.router._route_envelope_to_channels") as mock_route:
            _handle_auto_action_recovery(env, _sm=sm, _verifier=verifier)
            routed_state = mock_route.call_args[0][0].state
            assert routed_state is IncidentState.RECOVERY_UNCERTAIN

    def test_uncertain_escalates_severity(self):
        env = _envelope(state=IncidentState.AUTO_ACTION_SUCCEEDED, severity_hint=AlertSeverity.WARN)
        sm = _make_sm_mock()
        verifier = _make_failing_verifier()
        with patch("alerting_service.notifiers.router._route_envelope_to_channels"):
            _handle_auto_action_recovery(env, _sm=sm, _verifier=verifier)
            uncertain_call = [
                c
                for c in sm.transition.call_args_list
                if c[0][1] is IncidentState.RECOVERY_UNCERTAIN
            ]
            assert uncertain_call, "RECOVERY_UNCERTAIN transition not called"
            kwargs = uncertain_call[0][1]
            assert kwargs.get("severity_hint") == AlertSeverity.HIGH

    def test_failed_verif_transition_does_not_route(self):
        env = _envelope(state=IncidentState.AUTO_ACTION_SUCCEEDED)
        sm = _make_sm_mock(verif_succeeds=False)
        verifier = _make_all_passed_verifier()
        with patch("alerting_service.notifiers.router._route_envelope_to_channels") as mock_route:
            _handle_auto_action_recovery(env, _sm=sm, _verifier=verifier)
            mock_route.assert_not_called()


class TestRouteIncidentAutoActionSucceeded:
    def test_auto_action_succeeded_does_not_call_route_directly(self):
        env = _envelope(state=IncidentState.AUTO_ACTION_SUCCEEDED)
        with (
            patch("alerting_service.notifiers.router._handle_auto_action_recovery") as mock_handle,
            patch("alerting_service.notifiers.router._route_envelope_to_channels") as mock_route,
        ):
            route_incident(env)
            mock_handle.assert_called_once_with(env)
            mock_route.assert_not_called()

    def test_other_state_does_not_call_handle(self):
        env = _envelope(state=IncidentState.DETECTED)
        with (
            patch("alerting_service.notifiers.router._handle_auto_action_recovery") as mock_handle,
            patch("alerting_service.notifiers.router._route_envelope_to_channels") as mock_route,
        ):
            route_incident(env)
            mock_handle.assert_not_called()
            mock_route.assert_called_once_with(env)


# ── P0.12 — route_legacy_alert() backward-compat shim ────────────────────


class TestRouteLegacyAlert:
    def test_wraps_dict_and_calls_route_incident(self):
        payload = {
            "event_name": "OOM_DETECTED",
            "message": "OOM kill on executor",
            "service": "execution-service",
            "component": "order-router",
            "severity": "critical",
        }
        with patch("alerting_service.notifiers.router.route_incident") as mock_route:
            route_legacy_alert(payload, fallback_service="execution-service")
            mock_route.assert_called_once()
            envelope = mock_route.call_args[0][0]
            assert isinstance(envelope, IncidentEnvelope)
            assert envelope.service == "execution-service"

    def test_legacy_pydantic_model_wrapped(self):
        from pydantic import BaseModel

        class LegacyAlert(BaseModel):
            event_name: str
            message: str
            service: str

        alert = LegacyAlert(
            event_name="CIRCUIT_BREAKER_OPEN",
            message="breaker open",
            service="risk-service",
        )
        with patch("alerting_service.notifiers.router.route_incident") as mock_route:
            route_legacy_alert(alert)
            mock_route.assert_called_once()
            envelope = mock_route.call_args[0][0]
            assert isinstance(envelope, IncidentEnvelope)
            assert envelope.service == "risk-service"

    def test_problem_type_derived_from_event_name(self):
        payload = {"event_name": "KILL_SWITCH_ACTIVATED", "service": "alerting-service"}
        with patch("alerting_service.notifiers.router.route_incident") as mock_route:
            route_legacy_alert(payload)
            envelope = mock_route.call_args[0][0]
            assert envelope.problem_type == "KILL_SWITCH_ACTIVATED"
