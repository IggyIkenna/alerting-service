"""alerting-service CLI entry point.

ServiceBootstrap handles all infrastructure boilerplate:
  LOG_LEVEL validation, mock mode check, config resolution, event sink
  wiring, observability setup, graceful shutdown, and lifecycle events.

Supported modes:
  --mode live   → AlertHandler drives PubSub subscriber loop until SIGTERM
  --mode batch  → AlertHandler runs one poll cycle (for testing / batch replay)

Standard args provided by ServiceCLI (no service code needed):
  --mode, --log-level

Service-specific operation:
  --operation alerts  → AlertHandler (multi-topic PubSub subscriber + router)
"""

from __future__ import annotations

from unified_trading_library import ServiceBootstrap

from alerting_service.cli.handlers.alert_handler import AlertHandler
from alerting_service.config import AlertingSystemConfig
from alerting_service.engine.mock_data_provider import run_mock_pipeline
from alerting_service.kill_switch_bus_subscriber import (
    on_bus_event as _kill_switch_bus_subscriber,
)

_SERVICE_NAME = "alerting-service"  # pragma: no cover


def main_service_cli() -> None:  # pragma: no cover
    """ServiceBootstrap entry point for alerting-service."""
    ServiceBootstrap(
        service_name=_SERVICE_NAME,
        operations={"alerts": AlertHandler},
        config=AlertingSystemConfig(),
        modes=["live", "batch"],
        add_date_args=False,
        add_category_arg=False,
        mock_pipeline_fn=_run_mock,
        kill_switch_subscriber=_kill_switch_bus_subscriber,
    ).run()


def _run_mock() -> int:  # pragma: no cover
    """Mock pipeline — delegates to engine mock provider."""
    return run_mock_pipeline()
