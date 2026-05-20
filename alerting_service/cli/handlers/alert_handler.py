"""AlertHandler — BaseModeHandler for alerting-service.

Wires the AlertSubscriber (multi-topic PubSub pull loop) into the
ServiceBootstrap framework. The alerting service is an event router, not
a pipeline service, so it uses BaseModeHandler directly rather than
UnifiedServiceHandler — the run() method drives its own subscriber loop
until shutdown is requested via SIGTERM/SIGINT.

IMPORT CONTRACT
---------------
This module imports from:
  1. unified_trading_library (UTL) — BaseModeHandler, GracefulShutdownHandler
  2. alerting_service.engine.orchestrator — alert evaluation pipeline
  3. alerting_service.config_reloaders — domain config hot-reload wiring
  4. alerting_service.config — AlertingSystemConfig

No direct imports from UCI, UMI, UEI, or URDI.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from unified_trading_library import BaseModeHandler, GracefulShutdownHandler, ServiceRuntime

from alerting_service.config import AlertingSystemConfig
from alerting_service.config_reloaders import (
    start_domain_config_reloaders,
    start_paging_credentials_reloader,
    stop_domain_config_reloaders,
    stop_paging_credentials_reloader,
)
from alerting_service.engine import orchestrator as alert_orchestrator

logger = logging.getLogger(__name__)


class AlertHandler(BaseModeHandler):
    """Drive the alert subscriber loop as a ServiceBootstrap operation.

    run():
      1. Resolve AlertingSystemConfig from runtime
      2. Start domain config reloaders (instruments, venues, alert-rules)
      3. Drive AlertSubscriber until GracefulShutdownHandler signals stop
      4. Stop domain config reloaders cleanly on shutdown

    Shard-level failure isolation is handled in engine/orchestrator.py —
    per-message errors are logged and skipped; the loop never crashes.
    """

    def __init__(
        self,
        config: dict[str, object],
        args: argparse.Namespace | None = None,
        runtime: ServiceRuntime | None = None,
    ) -> None:
        super().__init__(config, args, runtime)

    def validate_config(self) -> bool:
        """Validate that project_id is set (required for PubSub access)."""
        cfg = self._resolve_config()
        if not cfg.gcp_project_id:
            logger.error("gcp_project_id is not set — cannot connect to PubSub")
            return False
        return True

    def _resolve_config(self) -> AlertingSystemConfig:
        """Extract AlertingSystemConfig from runtime or construct fresh."""
        runtime_config = self.config.get("_runtime")
        if isinstance(runtime_config, AlertingSystemConfig):
            return runtime_config
        # ServiceCLI injects the static config object under '_runtime' key
        # if it is the same type as config. Try the raw config dict fallback.
        raw_cfg = self.config.get("config")
        if isinstance(raw_cfg, AlertingSystemConfig):
            return raw_cfg
        # Construct from environment (all AlertingSystemConfig fields come from
        # UnifiedCloudConfig which reads from env vars automatically)
        return AlertingSystemConfig()

    async def run(self) -> dict[str, object]:
        """Run alert subscriber loop until shutdown is requested.

        Returns a result dict with status and event counts suitable for
        ServiceCLI's exit-code logic.
        """
        cfg = self._resolve_config()

        # Start domain config hot-reload wiring + SM paging credentials
        start_domain_config_reloaders(cfg)
        start_paging_credentials_reloader(cfg)

        shutdown_handler = GracefulShutdownHandler()

        if cfg.run_duration_hours > 0:
            logger.info(
                "Auto-shutdown scheduled in %dh (run_duration_hours)", cfg.run_duration_hours
            )
            asyncio.get_event_loop().call_later(
                cfg.run_duration_hours * 3600,
                shutdown_handler.request_shutdown,
            )

        if cfg.quietness_baseline_mode:
            logger.info(
                "QUIETNESS_BASELINE_MODE active — PagerDuty suppressed,"
                " routing to Telegram staging only"
            )

        try:
            total_processed = await alert_orchestrator.run_subscriber_loop(
                project_id=cfg.gcp_project_id,
                shutdown_handler=shutdown_handler,
            )
            return {"status": "ok", "events_processed": total_processed}
        except (OSError, ConnectionError, RuntimeError) as exc:
            logger.error("AlertHandler run failed: %s", exc)
            return {"status": "error", "message": str(exc)}
        finally:
            stop_domain_config_reloaders()
            stop_paging_credentials_reloader()
