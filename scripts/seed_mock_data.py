#!/usr/bin/env python3
"""Generate mock alert data for local dev / CI mock mode.

alerting-service is Layer 7 (monitoring/alerting) of the mock data dependency
chain. It consumes risk events, circuit breaker triggers, DeFi health warnings,
and execution anomalies from upstream services.

Upstream dependencies (Layer 6):
  - risk-and-exposure-service  (risk threshold breaches)
  - position-balance-monitor-service  (balance discrepancy alerts)
  - pnl-attribution-service  (P&L anomalies)

Usage:
    python scripts/seed_mock_data.py --scenario normal --seed 42 --env local
    python scripts/seed_mock_data.py --scenario heavy --seed 42 --env local
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from unified_api_contracts.internal import AlertEvent
from unified_api_contracts.internal.modes import MockScenario
from unified_trading_library.core.seed_writer import LocalWriter, SeedWriter, get_seed_writer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_NAME: Final[str] = "alerting-service"
LAYER: Final[int] = 7

# Alert categories matching the service's actual rule engine
ALERT_CATEGORIES: Final[list[str]] = [
    "risk_threshold_breach",
    "circuit_breaker_event",
    "defi_health_factor_warning",
    "execution_anomaly",
    "data_staleness",
    "kill_switch",
]

# Venues matching the service's subscriber topics
VENUES: Final[list[str]] = [
    "binance",
    "okx",
    "bybit",
    "deribit",
    "coinbase",
    "hyperliquid",
    "aave_v3",
]

STRATEGIES: Final[list[str]] = [
    "momentum-macd-v2",
    "breakout-v1",
    "mean-reversion-v3",
    "NG_STRADDLE_TRENDING",
    "CL_RISK_REVERSAL_TRENDING",
]

# Scenario-driven alert counts
SCENARIO_COUNTS: Final[dict[str, int]] = {
    "normal": 15,
    "heavy": 40,
    "light": 5,
    "big_ranges": 20,
    "bust": 30,
    "no_system_overload": 8,
    "missing_data": 12,
    "delayed_data": 18,
}


# ---------------------------------------------------------------------------
# Alert generators
# ---------------------------------------------------------------------------


def _generate_risk_threshold_breach(
    idx: int,
    base_time: datetime,
    rng_values: list[float],
) -> AlertEvent:
    """Generate a risk threshold breach alert (from risk-and-exposure-service)."""
    severity_pick = rng_values[0]
    severity = "CRITICAL" if severity_pick > 0.7 else "WARNING"
    venue = VENUES[int(rng_values[1] * len(VENUES)) % len(VENUES)]
    strategy = STRATEGIES[int(rng_values[2] * len(STRATEGIES)) % len(STRATEGIES)]
    metric_val = round(-1.0 - rng_values[3] * 4.0, 2)  # -1.0 to -5.0
    threshold_val = round(-1.0 - rng_values[4] * 2.0, 2)  # -1.0 to -3.0

    pos_pct = f"{abs(metric_val) * 20:.0f}"
    pos_thresh = f"{abs(threshold_val) * 20:.0f}"
    margin_pct = f"{abs(metric_val) * 15:.0f}"
    var_val = f"{abs(metric_val) * 10000:.0f}"
    var_lim = f"{abs(threshold_val) * 10000:.0f}"
    messages = [
        f"Max drawdown {metric_val}% exceeded {threshold_val}% threshold on {venue}",
        f"Position limit {pos_pct}% utilized (threshold {pos_thresh}%)",
        f"Margin utilization {margin_pct}% approaching limit on {venue}",
        f"VaR breach: portfolio VaR ${var_val} > limit ${var_lim}",
    ]
    msg = messages[int(rng_values[5] * len(messages)) % len(messages)]

    return AlertEvent(
        alert_id=f"alert-risk-{idx:03d}",
        rule_id="RISK_THRESHOLD_BREACH",
        triggered_at=base_time - timedelta(minutes=int(rng_values[6] * 480)),
        severity=severity,
        message=msg,
        metric_value=metric_val,
        threshold=threshold_val,
        strategy_id=strategy,
        venue=venue,
    )


def _generate_circuit_breaker_event(
    idx: int,
    base_time: datetime,
    rng_values: list[float],
) -> AlertEvent:
    """Generate a circuit breaker alert (CIRCUIT_BREAKER_OPEN / KILL_SWITCH_ACTIVATED)."""
    is_kill_switch = rng_values[0] > 0.6
    rule_id = "KILL_SWITCH_ACTIVATED" if is_kill_switch else "CIRCUIT_BREAKER_OPEN"
    error_rate = round(8.0 + rng_values[1] * 15.0, 1)
    threshold = 10.0

    if is_kill_switch:
        msg = f"Kill switch activated: daily loss limit reached ({error_rate}% drawdown)"
    else:
        service_names = ["execution-service", "strategy-service", "ml-inference-service"]
        svc = service_names[int(rng_values[2] * len(service_names)) % len(service_names)]
        msg = f"Circuit breaker opened for {svc}: error rate {error_rate}% > {threshold}%"

    return AlertEvent(
        alert_id=f"alert-cb-{idx:03d}",
        rule_id=rule_id,
        triggered_at=base_time - timedelta(minutes=int(rng_values[3] * 240)),
        severity="CRITICAL",
        message=msg,
        metric_value=error_rate,
        threshold=threshold,
        strategy_id=None,
        venue=None,
    )


def _generate_defi_health_factor_warning(
    idx: int,
    base_time: datetime,
    rng_values: list[float],
) -> AlertEvent:
    """Generate a DeFi health factor / protocol alert."""
    defi_alert_types = [
        ("DEFI_HEALTH_FACTOR_CRITICAL", "health_factor_critical"),
        ("DEFI_WEETH_DEPEG", "weeth_depeg"),
        ("DEFI_AAVE_UTILIZATION_SPIKE", "aave_utilization_spike"),
        ("DEFI_FUNDING_RATE_FLIP", "funding_rate_flip"),
        ("DEFI_FEATURE_STALE", "feature_stale"),
    ]
    pick = int(rng_values[0] * len(defi_alert_types)) % len(defi_alert_types)
    rule_id, alert_type = defi_alert_types[pick]

    protocols = ["aave_v3", "hyperliquid", "lido_wrapped", "uniswap_v3"]
    protocol = protocols[int(rng_values[1] * len(protocols)) % len(protocols)]
    assets = ["WETH", "USDC", "weETH", "ETH-USDC", "DAI"]
    asset = assets[int(rng_values[2] * len(assets)) % len(assets)]

    severity = "CRITICAL" if pick < 2 else "WARNING"

    hf_val = f"{0.8 + rng_values[3] * 0.4:.2f}"
    depeg_rate = f"{0.95 + rng_values[3] * 0.03:.4f}"
    depeg_dev = f"{2.5 + rng_values[4] * 3.0:.1f}"
    util_pct = f"{95 + rng_values[3] * 4:.1f}"
    fund_rate = f"{-0.001 - rng_values[3] * 0.005:.4f}"
    stale_age = f"{120 + rng_values[3] * 600:.0f}"
    messages_by_type: dict[str, str] = {
        "health_factor_critical": (
            f"Health factor {hf_val} on {protocol} (asset={asset}) -- liquidation risk"
        ),
        "weeth_depeg": (f"weETH depeg detected: rate={depeg_rate} (deviation={depeg_dev}%)"),
        "aave_utilization_spike": (
            f"Aave utilization spike: {asset} pool at {util_pct}% on {protocol}"
        ),
        "funding_rate_flip": (
            f"Funding rate flip: {asset} rate={fund_rate} on {protocol} -- shorts paying longs"
        ),
        "feature_stale": (f"DeFi feature stale: {alert_type} age={stale_age}s (SLA=60s)"),
    }
    msg = messages_by_type[alert_type]

    metric_val = round(rng_values[3] * 100, 2)
    threshold_val = round(rng_values[4] * 50 + 10, 2)

    return AlertEvent(
        alert_id=f"alert-defi-{idx:03d}",
        rule_id=rule_id,
        triggered_at=base_time - timedelta(minutes=int(rng_values[5] * 360)),
        severity=severity,
        message=msg,
        metric_value=metric_val,
        threshold=threshold_val,
        strategy_id=None,
        venue=protocol,
    )


def _generate_execution_anomaly(
    idx: int,
    base_time: datetime,
    rng_values: list[float],
) -> AlertEvent:
    """Generate an execution anomaly alert (order rejections, latency, slippage)."""
    anomaly_types = [
        "ORDER_REJECTION_SPIKE",
        "EXECUTION_LATENCY_BREACH",
        "SLIPPAGE_ANOMALY",
        "FILL_RATE_DEVIATION",
        "PREFLIGHT_FAILED",
    ]
    pick = int(rng_values[0] * len(anomaly_types)) % len(anomaly_types)
    rule_id = anomaly_types[pick]
    venue = VENUES[int(rng_values[1] * len(VENUES)) % len(VENUES)]
    strategy = STRATEGIES[int(rng_values[2] * len(STRATEGIES)) % len(STRATEGIES)]

    rej_rate = f"{15 + rng_values[3] * 30:.0f}"
    lat_val = f"{500 + rng_values[3] * 2000:.0f}"
    slip_val = f"{10 + rng_values[3] * 40:.1f}"
    fill_pct = f"{70 + rng_values[3] * 20:.0f}"
    messages_map: dict[str, str] = {
        "ORDER_REJECTION_SPIKE": (f"Order rejection rate {rej_rate}% on {venue} (threshold 10%)"),
        "EXECUTION_LATENCY_BREACH": (f"Execution latency p99={lat_val}ms on {venue} (SLO 500ms)"),
        "SLIPPAGE_ANOMALY": (f"Slippage {slip_val}bps on {venue} (expected <10bps)"),
        "FILL_RATE_DEVIATION": (f"Fill rate {fill_pct}% on {venue} (expected >95%)"),
        "PREFLIGHT_FAILED": (
            f"Preflight check failed for {strategy}: insufficient liquidity on {venue}"
        ),
    }
    msg = messages_map[rule_id]

    return AlertEvent(
        alert_id=f"alert-exec-{idx:03d}",
        rule_id=rule_id,
        triggered_at=base_time - timedelta(minutes=int(rng_values[4] * 480)),
        severity="WARNING" if pick > 1 else "CRITICAL",
        message=msg,
        metric_value=round(rng_values[3] * 100, 2),
        threshold=round(rng_values[5] * 50 + 5, 2),
        strategy_id=strategy,
        venue=venue,
    )


def _generate_data_staleness(
    idx: int,
    base_time: datetime,
    rng_values: list[float],
) -> AlertEvent:
    """Generate a data staleness alert."""
    venue = VENUES[int(rng_values[0] * len(VENUES)) % len(VENUES)]
    instruments = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BTC-PERPETUAL", "ETH-USDC"]
    instrument = instruments[int(rng_values[1] * len(instruments)) % len(instruments)]
    age_seconds = round(30 + rng_values[2] * 3600, 1)
    threshold_seconds = 30.0

    return AlertEvent(
        alert_id=f"alert-stale-{idx:03d}",
        rule_id="DATA_STALE",
        triggered_at=base_time - timedelta(minutes=int(rng_values[3] * 360)),
        severity="WARNING",
        message=(
            f"Market data stale for {venue} {instrument}:"
            f" last update {age_seconds:.0f}s ago"
            f" (threshold {threshold_seconds:.0f}s)"
        ),
        metric_value=age_seconds,
        threshold=threshold_seconds,
        strategy_id=None,
        venue=venue,
    )


# ---------------------------------------------------------------------------
# Delivery records
# ---------------------------------------------------------------------------

DELIVERY_CHANNELS: Final[list[str]] = ["pagerduty", "telegram", "slack"]


def _generate_delivery_records(
    alerts: list[AlertEvent],
    rng_seed: int,
) -> list[dict[str, str | bool]]:
    """Generate delivery records for each alert (mimicking router.py output)."""
    rng = random.Random(rng_seed)
    records: list[dict[str, str | bool]] = []

    for alert in alerts:
        # Critical alerts go to pagerduty + telegram, others just telegram
        channels = ["pagerduty", "telegram"] if alert.severity == "CRITICAL" else ["telegram"]
        for channel in channels:
            success = rng.random() > 0.1  # 90% success rate
            records.append(
                {
                    "delivery_id": f"del-{uuid.uuid4().hex[:6]}",
                    "alert_id": alert.alert_id,
                    "channel": channel,
                    "status": "delivered" if success else "failed",
                    "delivered_at": (
                        alert.triggered_at + timedelta(seconds=rng.randint(1, 5))
                    ).isoformat()
                    if success
                    else "",
                    "acknowledged": rng.random() > 0.5 if success else False,
                }
            )

    return records


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------

# Generator function type: (idx, base_time, rng_values) -> AlertEvent
_AlertGenerator = Callable[[int, datetime, list[float]], AlertEvent]

# Map category names to generator functions
_GENERATORS: dict[str, _AlertGenerator] = {
    "risk_threshold_breach": _generate_risk_threshold_breach,
    "circuit_breaker_event": _generate_circuit_breaker_event,
    "defi_health_factor_warning": _generate_defi_health_factor_warning,
    "execution_anomaly": _generate_execution_anomaly,
    "data_staleness": _generate_data_staleness,
    "kill_switch": _generate_circuit_breaker_event,
}


def generate_alerts(
    scenario: str,
    seed: int,
    date_str: str,
) -> tuple[list[AlertEvent], list[dict[str, str | bool]], dict[str, int]]:
    """Generate mock alerts and delivery records.

    Returns:
        Tuple of (alerts, delivery_records, category_counts).
    """
    rng = random.Random(seed)
    total_count = SCENARIO_COUNTS.get(scenario, 15)
    base_time = datetime.fromisoformat(f"{date_str}T12:00:00+00:00")
    alerts: list[AlertEvent] = []
    counts: dict[str, int] = dict.fromkeys(ALERT_CATEGORIES, 0)

    # Distribute alerts across categories with realistic weighting
    weights = {
        "risk_threshold_breach": 0.25,
        "circuit_breaker_event": 0.10,
        "defi_health_factor_warning": 0.20,
        "execution_anomaly": 0.20,
        "data_staleness": 0.15,
        "kill_switch": 0.10,
    }

    for i in range(total_count):
        # Select category based on weights
        r = rng.random()
        cumulative = 0.0
        category = "risk_threshold_breach"
        for cat, weight in weights.items():
            cumulative += weight
            if r <= cumulative:
                category = cat
                break

        # Generate random values for the generator
        rng_values = [rng.random() for _ in range(8)]

        gen_fn = _GENERATORS[category]
        alert = gen_fn(i, base_time, rng_values)
        alerts.append(alert)
        counts[category] += 1

    # Sort alerts by triggered_at descending (most recent first)
    alerts.sort(key=lambda a: a.triggered_at, reverse=True)

    # Generate delivery records
    delivery_records = _generate_delivery_records(alerts, seed)

    log.info(
        "Generated %d alerts: %s",
        len(alerts),
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v > 0),
    )

    return alerts, delivery_records, counts


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def write_alerts_json(
    alerts: list[AlertEvent],
    output_root: Path,
) -> Path:
    """Write alerts as a JSON array."""
    out_dir = output_root / "alerts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "recent_alerts.json"

    data = [alert.model_dump(mode="json") for alert in alerts]
    out_path.write_text(json.dumps(data, indent=2, default=str))
    log.info("Alerts: %d records -> %s", len(alerts), out_path)
    return out_path


def write_delivery_records(
    records: list[dict[str, str | bool]],
    output_root: Path,
) -> Path:
    """Write delivery records as JSON."""
    out_dir = output_root / "delivery"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "delivery_records.json"

    out_path.write_text(json.dumps(records, indent=2, default=str))
    log.info("Delivery records: %d records -> %s", len(records), out_path)
    return out_path


def write_routing_config(output_root: Path) -> Path:
    """Write a snapshot of routing rules (matching router.py defaults)."""
    out_dir = output_root / "config"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "routing_rules.json"

    rules: list[dict[str, object]] = [
        {
            "event_pattern": "KILL_SWITCH_*",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        {
            "event_pattern": "CIRCUIT_BREAKER_OPEN",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        {
            "event_pattern": "DEFI_HEALTH_FACTOR_CRITICAL",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        {
            "event_pattern": "DEFI_WEETH_DEPEG",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        {
            "event_pattern": "RISK_THRESHOLD_BREACH",
            "channels": ["pagerduty", "telegram"],
            "severity_filter": "critical",
        },
        {"event_pattern": "DEFI_*", "channels": ["telegram"], "severity_filter": "warning"},
        {"event_pattern": "MODEL_TRAINED", "channels": ["telegram"], "severity_filter": None},
        {"event_pattern": "PREFLIGHT_FAILED", "channels": ["telegram"], "severity_filter": None},
        {"event_pattern": "*", "channels": ["telegram"], "severity_filter": None},
    ]

    out_path.write_text(json.dumps({"routing_rules": rules}, indent=2, default=str))
    log.info("Routing config -> %s", out_path)
    return out_path


def write_seed_manifest(
    output_root: Path,
    counts: dict[str, int],
    written_paths: list[Path],
    scenario_name: str,
    seed: int,
    date_str: str,
) -> Path:
    """Write seed manifest JSON."""
    manifest: dict[str, object] = {
        "service": SERVICE_NAME,
        "layer": LAYER,
        "scenario": scenario_name,
        "seed": seed,
        "date": date_str,
        "generated_at": datetime.now(UTC).isoformat(),
        "alert_counts": counts,
        "total_alerts": sum(counts.values()),
        "files": [str(p) for p in written_paths],
    }

    manifest_path = output_root / "seed_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    log.info("Manifest: %s", manifest_path)
    return manifest_path


def write_seed_complete_marker(writer: SeedWriter) -> str:
    """Write .seed-complete marker file."""
    marker_data = json.dumps(
        {
            "service": SERVICE_NAME,
            "completed_at": datetime.now(UTC).isoformat(),
            "layer": LAYER,
        }
    )
    return writer.write_text(marker_data, ".seed-complete")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Generate mock alert data for local dev / CI mock mode.",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default="normal",
        choices=[s.value for s in MockScenario],
        help="Named scenario for deterministic mock generation (default: normal)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for deterministic output (default: 42)",
    )
    parser.add_argument(
        "--env",
        type=str,
        default="local",
        choices=["local", "dev"],
        help="Target environment (default: local)",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=datetime.now(UTC).strftime("%Y-%m-%d"),
        help="Date string for alert timestamps (default: today)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="",
        help="Override output directory (default: .local-dev-cache/mock-seed/alerting-service)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for seed_mock_data."""
    args = parse_args(argv)

    log.info("=== alerting-service mock data seed ===")
    log.info("Scenario: %s (seed=%d)", args.scenario, args.seed)
    log.info("Env: %s, Date: %s", args.env, args.date)

    # Create seed writer (local filesystem or cloud storage)
    writer = get_seed_writer(args.env, SERVICE_NAME, args.output_dir)
    log.info("Writer: %s", type(writer).__name__)

    # Keep output_root reference for internal write functions
    output_root = writer.base if isinstance(writer, LocalWriter) else Path(".")

    # Generate alerts and delivery records
    alerts, delivery_records, counts = generate_alerts(
        scenario=args.scenario,
        seed=args.seed,
        date_str=args.date,
    )

    # Write output files
    written: list[Path] = []
    written.append(write_alerts_json(alerts, output_root))
    written.append(write_delivery_records(delivery_records, output_root))
    written.append(write_routing_config(output_root))

    # Write manifest and marker
    write_seed_manifest(output_root, counts, written, args.scenario, args.seed, args.date)
    write_seed_complete_marker(writer)

    # Print summary
    total = sum(counts.values())
    log.info("=== SEED COMPLETE ===")
    log.info("Total alerts: %d", total)
    for cat, count in sorted(counts.items()):
        if count > 0:
            log.info("  %-30s %d", cat, count)
    log.info("Delivery records: %d", len(delivery_records))
    log.info("Files written: %d", len(written))
    log.info("Marker: %s/.seed-complete", output_root)

    return 0


if __name__ == "__main__":
    sys.exit(main())
