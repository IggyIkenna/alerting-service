#!/usr/bin/env python3
"""Synthetic alert injector for Phase 8 live-rehearsal.

Emits a DefiAlert with ``synthetic=True`` in details for each AlertCode (or a
single code via ``--code``). The alerting-service router suppresses PagerDuty /
Telegram dispatch for synthetic events but still logs + persists the delivery
record — so operators can verify the full routing path without generating real
pages.

Usage:
    # Inject all 76 alert codes sequentially (rehearsal mode):
    python scripts/inject_synthetic_alert.py

    # Inject a single code:
    python scripts/inject_synthetic_alert.py --code KILL_SWITCH_DEFI_LIQUIDATION_RISK

    # List available codes:
    python scripts/inject_synthetic_alert.py --list

    # Verbose output (show details dict per injection):
    python scripts/inject_synthetic_alert.py --verbose

Phase 8 of alerting_service_live_rules_2026_05_07.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from unified_api_contracts import AlertCode, DefiAlertType
from unified_api_contracts.internal import DefiAlert
from unified_trading_library import setup_events

from alerting_service.rules.defi_rules import route_defi_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Severity override for codes that warrant CRITICAL in rehearsal
_CRITICAL_CODES: frozenset[AlertCode] = frozenset(
    {
        AlertCode.KILL_SWITCH_DEFI_LIQUIDATION_RISK,
        AlertCode.KILL_SWITCH_PORTFOLIO_DRAWDOWN,
        AlertCode.KILL_SWITCH_VENUE_DISCONNECT,
        AlertCode.KILL_SWITCH_ML_MODEL_FAILURE,
        AlertCode.KILL_SWITCH_ORACLE_DIVERGENCE,
        AlertCode.DEFI_HEALTH_FACTOR_CRITICAL,
        AlertCode.DEFI_LIQUIDATION_IMMINENT,
        AlertCode.DEFI_POSITION_LIQUIDATED,
        AlertCode.CIRCUIT_BREAKER_OPEN,
        AlertCode.MARGIN_LIQUIDATION,
    }
)

# Map AlertCode → closest DefiAlertType (only used for metadata; event_name is
# always derived from alert.code.value when code is non-None).
_CODE_TO_ALERT_TYPE: dict[AlertCode, DefiAlertType] = {
    AlertCode.DEFI_HEALTH_FACTOR_CRITICAL: DefiAlertType.HEALTH_FACTOR_CRITICAL,
    AlertCode.DEFI_LIQUIDATION_IMMINENT: DefiAlertType.HEALTH_FACTOR_CRITICAL,
    AlertCode.DEFI_POSITION_LIQUIDATED: DefiAlertType.POSITION_LIQUIDATED,
    AlertCode.DEFI_WEETH_DEPEG: DefiAlertType.WEETH_DEPEG,
    AlertCode.DEFI_AAVE_UTILIZATION_SPIKE: DefiAlertType.AAVE_UTILIZATION_SPIKE,
    AlertCode.DEFI_FUNDING_RATE_FLIP: DefiAlertType.FUNDING_RATE_FLIP,
    AlertCode.DEFI_FEATURE_STALE: DefiAlertType.FEATURE_STALE,
    AlertCode.DEFI_TX_SIMULATION_FAILED: DefiAlertType.TX_SIMULATION_FAILED,
    AlertCode.DEFI_RATE_DEVIATION: DefiAlertType.RATE_DEVIATION,
}
_DEFAULT_ALERT_TYPE = DefiAlertType.HEALTH_FACTOR_CRITICAL


def _inject(code: AlertCode, *, verbose: bool = False) -> None:
    severity = "critical" if code in _CRITICAL_CODES else "warning"
    alert_type = _CODE_TO_ALERT_TYPE.get(code, _DEFAULT_ALERT_TYPE)
    details: dict[str, str | int | float | bool] = {
        "synthetic": True,
        "injected_by": "inject_synthetic_alert.py",
        "rehearsal_code": code.value,
    }
    alert = DefiAlert(
        alert_type=alert_type,
        severity=severity,
        message=f"[SYNTHETIC] Phase 8 rehearsal: {code.value}",
        code=code,
        details=details,
    )
    if verbose:
        log.info("Injecting code=%s severity=%s type=%s", code.value, severity, alert_type.value)
    route_defi_alert(alert)
    log.info("INJECTED code=%s (synthetic — paging suppressed)", code.value)


def _list_codes() -> None:
    for code in AlertCode:
        severity_tag = "CRITICAL" if code in _CRITICAL_CODES else "warning"
        print(f"  {code.value:<55}  [{severity_tag}]")


def main() -> None:
    setup_events(service_name="alerting-service", mode="local")

    parser = argparse.ArgumentParser(
        description="Inject synthetic DefiAlerts for Phase 8 rehearsal."
    )
    parser.add_argument(
        "--code",
        metavar="CODE",
        help="Inject a single AlertCode value (e.g. KILL_SWITCH_DEFI_LIQUIDATION_RISK). "
        "Omit to inject all codes.",
    )
    parser.add_argument("--list", action="store_true", help="List all AlertCode values and exit.")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        metavar="SECONDS",
        help="Delay between injections when running all codes (default 0.1s).",
    )
    parser.add_argument("--verbose", action="store_true", help="Log details dict per injection.")
    args = parser.parse_args()

    if args.list:
        _list_codes()
        return

    if args.code:
        try:
            code = AlertCode(args.code)
        except ValueError:
            log.error("Unknown AlertCode %r. Use --list to see valid values.", args.code)
            sys.exit(1)
        _inject(code, verbose=args.verbose)
        return

    codes = list(AlertCode)
    log.info("Injecting %d synthetic alerts (delay=%.2fs each) …", len(codes), args.delay)
    for i, code in enumerate(codes, 1):
        _inject(code, verbose=args.verbose)
        if i < len(codes) and args.delay > 0:
            time.sleep(args.delay)
    log.info("Done — %d synthetic alerts injected. All paging suppressed.", len(codes))


if __name__ == "__main__":
    main()
