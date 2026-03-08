"""Package entry point: python -m alerting_service."""

import asyncio

from .main import main

asyncio.run(main())
