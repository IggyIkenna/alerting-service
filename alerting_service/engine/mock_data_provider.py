"""Mock data provider for alerting-service.

In mock mode (CLOUD_MOCK_MODE=true), loads mock risk metrics from
risk-and-exposure-service seed, runs REAL threshold evaluation logic
to determine alert severity, and writes alert records to the seed
directory. Notification delivery (Slack/PagerDuty) is skipped.

Reads upstream from:
    .local-dev-cache/mock-seed/risk-and-exposure-service/

Writes output to:
    .local-dev-cache/mock-seed/alerting-service/
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from alerting_service.rules.risk_threshold_rules import evaluate_risk_thresholds

logger = logging.getLogger(__name__)

SERVICE_NAME: Final[str] = "alerting-service"
UPSTREAM_SERVICE: Final[str] = "risk-and-exposure-service"
LAYER: Final[int] = 7


def _get_workspace_root() -> Path:
    """Resolve workspace root relative to this file's location.

    alerting_service/engine/mock_data_provider.py is 4 levels deep from
    the workspace root: workspace/alerting-service/alerting_service/engine/
    """
    return Path(__file__).resolve().parents[3]


def _get_seed_base(service: str = SERVICE_NAME) -> Path:
    """Return the seed data directory for a service."""
    return _get_workspace_root() / ".local-dev-cache" / "mock-seed" / service


def _upstream_available() -> bool:
    """Check if upstream seed data is available."""
    marker = _get_seed_base(UPSTREAM_SERVICE) / ".seed-complete"
    return marker.exists()


def _load_upstream_risk_metrics() -> dict[str, object]:
    """Load risk metrics from upstream risk-and-exposure-service seed."""
    metrics_path = _get_seed_base(UPSTREAM_SERVICE) / "metrics" / "risk_metrics.json"
    if metrics_path.exists():
        raw = json.loads(metrics_path.read_text())
        return cast(dict[str, object], raw)
    return {}


def run_mock_pipeline() -> int:
    """Run the mock pipeline for alerting-service.

    1. Load risk metrics from risk-and-exposure-service
    2. Run REAL threshold evaluation
    3. Write alerts to seed directory (skip notification delivery)

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    seed_base = _get_seed_base()
    marker = seed_base / ".seed-complete"

    if marker.exists():
        logger.info(
            "MOCK MODE: Seed data already present at %s -- skipping generation",
            seed_base,
        )
        return 0

    upstream_ok = _upstream_available()
    logger.info("MOCK MODE: upstream=%s available=%s", UPSTREAM_SERVICE, upstream_ok)

    risk_metrics = _load_upstream_risk_metrics()
    if risk_metrics:
        logger.info("MOCK MODE: Loaded risk metrics for client=%s", risk_metrics.get("client_id"))
    else:
        # Fallback with values that will trigger some alerts
        risk_metrics = {
            "client_id": "mock-client",
            "leverage": "8.5",
            "concentration": "0.42",
            "drawdown": "0.12",
            "account_equity": "53000",
        }
        logger.info("MOCK MODE: Using fallback risk metrics")

    # Run REAL threshold evaluation from alerting_service.rules
    alerts = evaluate_risk_thresholds(risk_metrics)
    logger.info(
        "MOCK MODE: Threshold evaluation produced %d alerts",
        len(alerts),
    )

    for alert in alerts:
        logger.info(
            "MOCK MODE: Alert: %s=%s severity=%s",
            alert["metric_name"],
            alert["metric_value"],
            alert["severity"],
        )

    # Write alerts
    seed_base.mkdir(parents=True, exist_ok=True)
    alerts_dir = seed_base / "alerts"
    alerts_dir.mkdir(parents=True, exist_ok=True)
    (alerts_dir / "alerts.json").write_text(json.dumps(alerts, indent=2))

    marker_data = json.dumps(
        {
            "service": SERVICE_NAME,
            "completed_at": datetime.now(UTC).isoformat(),
            "layer": LAYER,
            "mock_mode": True,
            "upstream_available": upstream_ok,
            "ran_real_threshold_eval": True,
            "alert_count": len(alerts),
        }
    )
    marker.write_text(marker_data)
    logger.info("MOCK MODE: %s pipeline complete", SERVICE_NAME)
    return 0
