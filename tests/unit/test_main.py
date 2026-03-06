"""Unit tests for alerting-system main entry point."""

import pytest

from alerting_system.main import main


@pytest.mark.asyncio
async def test_main_runs_successfully() -> None:
    """Test that main() runs without error (happy path)."""
    await main()
    assert True, "Main function completed without error"
