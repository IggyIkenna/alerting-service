"""
Main entry point for alerting-system
"""

import asyncio

from unified_events_interface import log_event, setup_events

from .config import AlertingSystemConfig


async def main() -> None:
    """Main service logic"""
    config = AlertingSystemConfig()

    # Setup event logging
    setup_events(
        service_name=config.service_name,
        mode="batch"  # or "live" based on mode
    )

    log_event("STARTED")

    try:
        # TODO: Implement service logic
        pass

        log_event("SUCCESS")
    except Exception as e:
        log_event("FAILED", severity="ERROR", details={"error": str(e)})
        raise


if __name__ == "__main__":
    asyncio.run(main())
