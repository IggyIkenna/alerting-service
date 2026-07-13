"""Unit tests for alerting_service.api.routes.health."""

from __future__ import annotations

from fastapi.testclient import TestClient

from alerting_service.api.main import app


class TestHealthEndpoint:
    def test_health_returns_ok(self) -> None:
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "ok"
        assert result["service"] == "alerting-service"
        assert "cloud_provider" in result
        assert "mock_mode" in result

    def test_health_data_freshness_is_dict(self) -> None:
        client = TestClient(app)
        response = client.get("/health")
        result = response.json()
        freshness = result.get("data_freshness")
        assert isinstance(freshness, dict), f"data_freshness must be dict, got {type(freshness)}"

    def test_health_data_freshness_fields(self) -> None:
        client = TestClient(app)
        response = client.get("/health")
        result = response.json()
        freshness = result["data_freshness"]
        assert isinstance(freshness.get("last_processed_date"), str)
        assert isinstance(freshness.get("stale"), bool)
