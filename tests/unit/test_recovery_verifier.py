"""Unit tests for P0.10 RecoveryVerifier — per-service callback dispatcher."""

from __future__ import annotations

from unified_api_contracts.incident import RecoveryVerificationResult

from alerting_service.gateway.recovery_verifier import RecoveryVerifier


def _all_pass() -> RecoveryVerificationResult:
    return RecoveryVerificationResult(
        health_checks_passed=True,
        positions_reconciled=True,
        orders_reconciled=True,
        market_data_fresh=True,
        strategy_state_restored=True,
    )


def _fail_positions() -> RecoveryVerificationResult:
    return RecoveryVerificationResult(
        health_checks_passed=True,
        positions_reconciled=False,
        orders_reconciled=True,
        market_data_fresh=True,
        strategy_state_restored=True,
        failure_reasons=("positions out of sync with venue",),
    )


class TestRecoveryVerifierNoCallbacks:
    def test_empty_registry_returns_all_true(self):
        verifier = RecoveryVerifier()
        result = verifier.verify("inc-001", {})
        assert result.all_passed

    def test_empty_registry_no_failure_reasons(self):
        verifier = RecoveryVerifier()
        result = verifier.verify("inc-001", {})
        assert result.failure_reasons == ()

    def test_registered_services_empty(self):
        assert RecoveryVerifier().registered_services == frozenset()


class TestRecoveryVerifierSingleCallback:
    def test_all_pass_callback_returns_all_true(self):
        verifier = RecoveryVerifier()
        verifier.register("execution-service", lambda scope: _all_pass())
        result = verifier.verify("inc-001", {"strategy_id": "S1"})
        assert result.all_passed
        assert result.failure_reasons == ()

    def test_failing_callback_propagates_false(self):
        verifier = RecoveryVerifier()
        verifier.register("execution-service", lambda scope: _fail_positions())
        result = verifier.verify("inc-001", {})
        assert not result.positions_reconciled
        assert result.health_checks_passed
        assert "positions out of sync" in result.failure_reasons[0]

    def test_scope_passed_to_callback(self):
        received: list[dict[str, str]] = []

        def capture_scope(scope: dict[str, str]) -> RecoveryVerificationResult:
            received.append(scope)
            return _all_pass()

        verifier = RecoveryVerifier()
        verifier.register("svc", capture_scope)
        verifier.verify("inc-x", {"venue": "binance"})
        assert received == [{"venue": "binance"}]


class TestRecoveryVerifierMultipleCallbacks:
    def test_all_services_pass(self):
        verifier = RecoveryVerifier()
        verifier.register("execution-service", lambda s: _all_pass())
        verifier.register("strategy-service", lambda s: _all_pass())
        verifier.register("mtds", lambda s: _all_pass())
        result = verifier.verify("inc-002", {})
        assert result.all_passed

    def test_one_service_fails_positions(self):
        verifier = RecoveryVerifier()
        verifier.register("execution-service", lambda s: _fail_positions())
        verifier.register("strategy-service", lambda s: _all_pass())
        result = verifier.verify("inc-003", {})
        assert not result.positions_reconciled
        assert result.health_checks_passed
        assert result.strategy_state_restored
        assert len(result.failure_reasons) == 1

    def test_failure_reasons_aggregated_across_services(self):
        verifier = RecoveryVerifier()
        verifier.register(
            "svc-a",
            lambda s: RecoveryVerificationResult(
                health_checks_passed=False,
                positions_reconciled=True,
                orders_reconciled=True,
                market_data_fresh=True,
                strategy_state_restored=True,
                failure_reasons=("svc-a health fail",),
            ),
        )
        verifier.register(
            "svc-b",
            lambda s: RecoveryVerificationResult(
                health_checks_passed=True,
                positions_reconciled=True,
                orders_reconciled=False,
                market_data_fresh=True,
                strategy_state_restored=True,
                failure_reasons=("svc-b orders stale",),
            ),
        )
        result = verifier.verify("inc-004", {})
        assert not result.health_checks_passed
        assert not result.orders_reconciled
        assert result.positions_reconciled
        assert "svc-a health fail" in result.failure_reasons
        assert "svc-b orders stale" in result.failure_reasons

    def test_booleans_are_anded_across_services(self):
        """If two services both fail market_data_fresh, result is False."""
        verifier = RecoveryVerifier()
        for name in ("mtds-a", "mtds-b"):
            verifier.register(
                name,
                lambda s: RecoveryVerificationResult(
                    health_checks_passed=True,
                    positions_reconciled=True,
                    orders_reconciled=True,
                    market_data_fresh=False,
                    strategy_state_restored=True,
                ),
            )
        result = verifier.verify("inc-005", {})
        assert not result.market_data_fresh


class TestRecoveryVerifierExceptions:
    def test_callback_exception_sets_all_false(self):
        verifier = RecoveryVerifier()

        def exploding(_: dict[str, str]) -> RecoveryVerificationResult:
            raise RuntimeError("connection refused")

        verifier.register("broken-service", exploding)
        result = verifier.verify("inc-006", {})
        assert not result.all_passed
        assert not result.health_checks_passed
        assert "broken-service" in result.failure_reasons[0]
        assert "RuntimeError" in result.failure_reasons[0]

    def test_exception_in_one_does_not_suppress_other_callback(self):
        verifier = RecoveryVerifier()

        verifier.register("broken", lambda s: (_ for _ in ()).throw(ValueError("oops")))
        verifier.register("healthy", lambda s: _all_pass())

        result = verifier.verify("inc-007", {})
        # broken makes all False; healthy passes its own fields → AND is False for all
        assert not result.all_passed
        assert len(result.failure_reasons) >= 1

    def test_re_registration_overwrites_old_callback(self):
        verifier = RecoveryVerifier()
        verifier.register("svc", lambda s: _fail_positions())
        # overwrite with passing callback
        verifier.register("svc", lambda s: _all_pass())
        result = verifier.verify("inc-008", {})
        assert result.all_passed

    def test_registered_services_property(self):
        verifier = RecoveryVerifier()
        verifier.register("execution-service", lambda s: _all_pass())
        verifier.register("strategy-service", lambda s: _all_pass())
        assert verifier.registered_services == frozenset({"execution-service", "strategy-service"})
