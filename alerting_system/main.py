"""
Alerting-system main entry point.
"""

import asyncio

from unified_events_interface import MockEventSink, log_event, setup_events

from .config import AlertingSystemConfig


async def main() -> None:
    """Main service logic"""
    config = AlertingSystemConfig()

    # Setup event logging (MockEventSink in dev; use GCSEventSink from UTS in prod batch)
    setup_events(service_name=config.service_name, mode="batch", sink=MockEventSink())

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
