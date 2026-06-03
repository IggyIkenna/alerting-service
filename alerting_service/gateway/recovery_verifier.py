"""Per-service recovery-verification callback dispatcher — P0.10.

Services register callbacks at gateway startup via ``RecoveryVerifier.register()``.
When an incident reaches RECOVERY_VERIFICATION_STARTED the router calls
``verify()`` to aggregate all registered callbacks into a single
``RecoveryVerificationResult``.

All 5 booleans must be True for the state machine to transition to
RECOVERY_CONFIRMED. Any False → RECOVERY_UNCERTAIN → SAFE_MODE_ACTIVE.

Registration example (in each service's gateway startup)::

    verifier.register(
        "execution-service",
        lambda scope: RecoveryVerificationResult(
            health_checks_passed=_ping_health(),
            positions_reconciled=_reconcile_positions(scope),
            orders_reconciled=_reconcile_orders(scope),
            market_data_fresh=True,          # not this service's domain
            strategy_state_restored=True,    # not this service's domain
        ),
    )
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from unified_api_contracts.incident import RecoveryVerificationResult

_logger = logging.getLogger(__name__)

_ALL_PASSED = RecoveryVerificationResult(
    health_checks_passed=True,
    positions_reconciled=True,
    orders_reconciled=True,
    market_data_fresh=True,
    strategy_state_restored=True,
)
_ALL_FAILED = RecoveryVerificationResult(
    health_checks_passed=False,
    positions_reconciled=False,
    orders_reconciled=False,
    market_data_fresh=False,
    strategy_state_restored=False,
)

type VerificationCallback = Callable[[dict[str, str]], RecoveryVerificationResult]
"""Callable registered by each service.

Takes a scope dict (e.g. ``{"strategy_id": "...", "venue": "binance"}``)
and returns a RecoveryVerificationResult. Fields not in the service's
responsibility domain MUST be returned as True (pass-through).
"""


class RecoveryVerifier:
    """Aggregates per-service recovery callbacks into a single verdict.

    Construct once per process and inject into the gateway router. The
    verifier is stateless beyond holding the callback registry.
    """

    def __init__(self) -> None:
        self._callbacks: dict[str, VerificationCallback] = {}

    def register(self, service_id: str, callback: VerificationCallback) -> None:
        """Register or replace a per-service verification callback.

        Thread-safe for Python's GIL; not guarded against concurrent
        registration races (assumed to happen at startup only).
        """
        self._callbacks[service_id] = callback

    def verify(self, incident_key: str, scope: dict[str, str]) -> RecoveryVerificationResult:
        """Run all registered callbacks and AND their results.

        Returns all-True when no callbacks are registered (vacuously passing
        — gate is disabled until at least one service registers).

        A callback that raises is treated as a total failure for that service:
        all 5 booleans flip to False and the exception message goes into
        ``failure_reasons``.
        """
        if not self._callbacks:
            return _ALL_PASSED

        health = positions = orders = market_data = strategy_state = True
        failure_reasons: list[str] = []
        for service_id, callback in self._callbacks.items():
            partial, reasons = self._invoke(service_id, callback, scope, incident_key)
            health = health and partial.health_checks_passed
            positions = positions and partial.positions_reconciled
            orders = orders and partial.orders_reconciled
            market_data = market_data and partial.market_data_fresh
            strategy_state = strategy_state and partial.strategy_state_restored
            failure_reasons.extend(reasons)

        return RecoveryVerificationResult(
            health_checks_passed=health,
            positions_reconciled=positions,
            orders_reconciled=orders,
            market_data_fresh=market_data,
            strategy_state_restored=strategy_state,
            failure_reasons=tuple(failure_reasons),
        )

    def _invoke(
        self,
        service_id: str,
        callback: VerificationCallback,
        scope: dict[str, str],
        incident_key: str,
    ) -> tuple[RecoveryVerificationResult, list[str]]:
        """Call one callback; return (result, extra_failure_reasons).

        On exception returns an all-False result so the aggregate fails safely.
        """
        try:
            result = callback(scope)
            return result, list(result.failure_reasons)
        except Exception as exc:
            msg = f"{service_id}: callback raised {type(exc).__name__}({exc})"
            _logger.exception("RecoveryVerifier callback failed: %s incident_key=%s", msg, incident_key)
            return _ALL_FAILED, [msg]

    @property
    def registered_services(self) -> frozenset[str]:
        """Return the set of service_ids with registered callbacks."""
        return frozenset(self._callbacks)
