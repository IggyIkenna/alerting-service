"""Mock data provider for alerting-service.

In mock mode (CLOUD_MOCK_MODE=true), reads pre-generated alerts and delivery records
from the seed directory instead of running the real pipeline.

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
from typing import Final

logger = logging.getLogger(__name__)

SERVICE_NAME: Final[str] = "alerting-service"
UPSTREAM_SERVICE: Final[str] = "risk-and-exposure-service"
LAYER: Final[int] = 7


def _get_workspace_root() -> Path:
    """Resolve workspace root from env or heuristics."""
    import os

    workspace = os.environ.get(
        "WORKSPACE_ROOT",
        os.environ.get("UNIFIED_TRADING_WORKSPACE_ROOT", ""),
    )
    if workspace:
        return Path(workspace)
    return Path(__file__).resolve().parents[3]


def _get_seed_base(service: str = SERVICE_NAME) -> Path:
    """Return the seed data directory for a service."""
    return _get_workspace_root() / ".local-dev-cache" / "mock-seed" / service


def _upstream_available() -> bool:
    """Check if upstream seed data is available."""
    marker = _get_seed_base(UPSTREAM_SERVICE) / ".seed-complete"
    return marker.exists()


def run_mock_pipeline() -> int:
    """Run the mock pipeline for alerting-service.

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

    seed_files = list(seed_base.rglob("*.parquet")) + list(seed_base.rglob("*.json"))
    if seed_files:
        logger.info(
            "MOCK MODE: Found %d pre-generated seed files in %s",
            len(seed_files),
            seed_base,
        )
    else:
        logger.info(
            "MOCK MODE: No pre-generated seed data -- run seed_mock_data.py first. "
            "Continuing with empty output."
        )
        seed_base.mkdir(parents=True, exist_ok=True)

    marker_data = json.dumps(
        {
            "service": SERVICE_NAME,
            "completed_at": datetime.now(UTC).isoformat(),
            "layer": LAYER,
            "mock_mode": True,
            "upstream_available": upstream_ok,
            "seed_files_count": len(seed_files),
        }
    )
    marker.write_text(marker_data)
    logger.info("MOCK MODE: %s pipeline complete", SERVICE_NAME)
    return 0
