"""Unit tests for the delivery status API endpoint."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from alerting_service.api.routes import delivery_status as ds_module
from alerting_service.api.routes.delivery_status import get_delivery_store
from alerting_service.auth import verify_api_key

# Suppress cloud_mock_mode deprecation warnings from monkeypatch.setattr
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def mock_storage_store() -> MagicMock:
    """Create a mock AlertStorageStore."""
    return MagicMock()


@pytest.fixture
def client_non_mock(mock_storage_store: MagicMock, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Create a FastAPI test client with mock mode disabled (tests real code path)."""
    from alerting_service.api.main import app

    monkeypatch.setattr(ds_module._cfg, "data_mode", "real")
    monkeypatch.setattr(ds_module._cfg, "cloud_mock_mode", False)

    async def _bypass_auth() -> str:
        return "test-key"

    app.dependency_overrides[verify_api_key] = _bypass_auth
    app.dependency_overrides[get_delivery_store] = lambda: mock_storage_store

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture
def client_mock_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Create a FastAPI test client with mock mode enabled (tests mock code path)."""
    from alerting_service.api.main import app

    monkeypatch.setattr(ds_module._cfg, "data_mode", "mock")

    async def _bypass_auth() -> str:
        return "test-key"

    app.dependency_overrides[verify_api_key] = _bypass_auth
    # Override the store dependency to avoid GCS credential resolution
    app.dependency_overrides[get_delivery_store] = lambda: MagicMock()

    yield TestClient(app)

    app.dependency_overrides.clear()


class TestDeliveryStatusEndpoint:
    def test_returns_records_for_alert_id(
        self,
        client_non_mock: TestClient,
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

        response = client_non_mock.get("/alerts/delivery-status/abc123")
        assert response.status_code == 200
        data = response.json()
        assert data["alert_id"] == "abc123"
        assert len(data["records"]) == 2

    def test_returns_empty_records_for_unknown_alert(
        self,
        client_non_mock: TestClient,
        mock_storage_store: MagicMock,
    ) -> None:
        mock_storage_store.read_delivery_records.return_value = []

        response = client_non_mock.get("/alerts/delivery-status/unknown")
        assert response.status_code == 200
        data = response.json()
        assert data["alert_id"] == "unknown"
        assert data["records"] == []

    def test_calls_read_delivery_records_with_correct_id(
        self,
        client_non_mock: TestClient,
        mock_storage_store: MagicMock,
    ) -> None:
        mock_storage_store.read_delivery_records.return_value = []

        client_non_mock.get("/alerts/delivery-status/my-alert-id")
        mock_storage_store.read_delivery_records.assert_called_once_with("my-alert-id")

    def test_returns_records_non_mock_post(
        self,
        client_non_mock: TestClient,
    ) -> None:
        record = {"alert_id": "abc", "channel": "slack", "status": "sent"}
        response = client_non_mock.post(
            "/alerts/delivery-status",
            json=record,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["alert_id"] == "abc"


class TestDeliveryStatusMockMode:
    """Tests for the mock-mode code path of delivery status."""

    def test_mock_mode_returns_sample_records(
        self,
        client_mock_mode: TestClient,
    ) -> None:
        response = client_mock_mode.get("/alerts/delivery-status/alert-xyz")
        assert response.status_code == 200
        data = response.json()
        assert data["alert_id"] == "alert-xyz"
        assert len(data["records"]) >= 1

    def test_mock_mode_create_delivery_record(
        self,
        client_mock_mode: TestClient,
    ) -> None:
        record = {
            "alert_id": "alert-abc",
            "channel": "telegram",
            "status": "delivered",
        }
        response = client_mock_mode.post(
            "/alerts/delivery-status",
            json=record,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["alert_id"] == "alert-abc"
        assert "delivery_id" in data
