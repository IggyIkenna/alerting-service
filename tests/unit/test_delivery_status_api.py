"""Unit tests for the delivery status API endpoint."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from alerting_service.api.routes.delivery_status import get_delivery_store
from alerting_service.auth import verify_api_key


@pytest.fixture
def mock_storage_store() -> MagicMock:
    """Create a mock AlertStorageStore."""
    return MagicMock()


@pytest.fixture
def client(mock_storage_store: MagicMock) -> Iterator[TestClient]:
    """Create a FastAPI test client with mocked dependencies."""
    from alerting_service.api.main import app

    async def _bypass_auth() -> str:
        return "test-key"

    app.dependency_overrides[verify_api_key] = _bypass_auth
    app.dependency_overrides[get_delivery_store] = lambda: mock_storage_store

    yield TestClient(app)

    app.dependency_overrides.clear()


class TestDeliveryStatusEndpoint:
    def test_returns_records_for_alert_id(
        self,
        client: TestClient,
        mock_storage_store: MagicMock,
    ) -> None:
        mock_storage_store.read_delivery_records.return_value = [
            {
                "alert_id": "abc123",
                "channel": "telegram",
                "status": "sent",
                "timestamp": "2026-03-14T10:00:00+00:00",
            },
            {
                "alert_id": "abc123",
                "channel": "pagerduty",
                "status": "sent",
                "timestamp": "2026-03-14T10:00:00+00:00",
            },
        ]

        response = client.get("/alerts/delivery-status/abc123")
        assert response.status_code == 200
        data = response.json()
        assert data["alert_id"] == "abc123"
        assert len(data["records"]) == 2

    def test_returns_empty_records_for_unknown_alert(
        self,
        client: TestClient,
        mock_storage_store: MagicMock,
    ) -> None:
        mock_storage_store.read_delivery_records.return_value = []

        response = client.get("/alerts/delivery-status/unknown")
        assert response.status_code == 200
        data = response.json()
        assert data["alert_id"] == "unknown"
        assert data["records"] == []

    def test_calls_read_delivery_records_with_correct_id(
        self,
        client: TestClient,
        mock_storage_store: MagicMock,
    ) -> None:
        mock_storage_store.read_delivery_records.return_value = []

        client.get("/alerts/delivery-status/my-alert-id")
        mock_storage_store.read_delivery_records.assert_called_once_with("my-alert-id")
