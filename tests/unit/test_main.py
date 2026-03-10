"""Unit tests for alerting-service main entry point."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerting_service.main import _build_parser, _run_subscriber_until_shutdown, main


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


class TestRunSubscriberUntilShutdown:
    """Tests for _run_subscriber_until_shutdown helper."""

    @pytest.mark.asyncio
    async def test_stops_when_shutdown_requested(self) -> None:
        """Subscriber is stopped once shutdown_handler reports shutdown requested."""
        subscriber = MagicMock()
        subscriber.run_until_stopped = AsyncMock(return_value=None)
        subscriber.stop = MagicMock()

        shutdown_handler = MagicMock()
        # First call returns False, second returns True — simulates signal arriving.
        shutdown_handler.is_shutdown_requested.side_effect = [False, True]

        await _run_subscriber_until_shutdown(subscriber, shutdown_handler, poll_interval=0.0)

        subscriber.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stops_when_subscriber_task_finishes(self) -> None:
        """Loop exits if the subscriber task completes on its own (e.g. empty stream)."""
        subscriber = MagicMock()
        # Coroutine that returns immediately.
        subscriber.run_until_stopped = AsyncMock(return_value=None)
        subscriber.stop = MagicMock()

        shutdown_handler = MagicMock()
        # Shutdown never requested — loop exits because subscriber_task is done.
        shutdown_handler.is_shutdown_requested.return_value = False

        await _run_subscriber_until_shutdown(subscriber, shutdown_handler, poll_interval=0.0)

        subscriber.stop.assert_called_once()


@pytest.mark.asyncio
async def test_main_runs_successfully() -> None:
    """Test that main() runs without error (happy path, all I/O mocked)."""
    with (
        patch.object(sys, "argv", ["alerting-service", "--mode", "batch"]),
        patch("alerting_service.main.PubSubEventSink"),
        patch("alerting_service.main.setup_service_observability"),
        patch("alerting_service.main.GracefulShutdownHandler") as mock_handler_cls,
        patch("alerting_service.main.log_event"),
        patch("alerting_service.main.AlertSubscriber") as mock_subscriber_cls,
        patch(
            "alerting_service.main._run_subscriber_until_shutdown", new_callable=AsyncMock
        ) as mock_run,
    ):
        mock_handler = MagicMock()
        mock_handler.is_shutdown_requested.return_value = True
        mock_handler_cls.return_value = mock_handler

        mock_subscriber = MagicMock()
        mock_subscriber_cls.return_value = mock_subscriber

        mock_run.return_value = None

        await main()

    mock_run.assert_awaited_once()
