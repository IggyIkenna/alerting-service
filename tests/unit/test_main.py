"""Unit tests for alerting-service main entry point."""

import sys
from unittest.mock import patch

import pytest

from alerting_service.main import _build_parser, main


class TestBuildParser:
    """Tests for _build_parser."""

    def test_mode_is_required(self) -> None:
        """--mode must be required with no default."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_mode_batch(self) -> None:
        """--mode batch is accepted."""
        parser = _build_parser()
        args = parser.parse_args(["--mode", "batch"])
        assert args.mode == "batch"

    def test_mode_live(self) -> None:
        """--mode live is accepted."""
        parser = _build_parser()
        args = parser.parse_args(["--mode", "live"])
        assert args.mode == "live"

    def test_mode_invalid(self) -> None:
        """Invalid mode values are rejected."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--mode", "invalid"])


@pytest.mark.asyncio
async def test_main_runs_successfully() -> None:
    """Test that main() runs without error (happy path)."""
    with patch.object(sys, "argv", ["alerting-service", "--mode", "batch"]):
        await main()
    assert True, "Main function completed without error"
