"""Service startup smoke tests for alerting-service.

T4f STEP B checklist:
- Package imports cleanly
- Config instantiates with CLOUD_PROVIDER=local
- API app is importable with /health and /readiness endpoints
- Alert subscriber module is importable
- Metrics module is importable
- PubSub access is via get_queue_client — no direct google-cloud-pubsub
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api_client():
    """TestClient with API key bypass."""
    with patch("alerting_service.auth.verify_api_key", return_value="test-key"):
        from alerting_service.api.main import app

        with TestClient(app) as tc:
            yield tc


@pytest.mark.unit
def test_package_config_importable() -> None:
    """Config module must be importable."""
    os.environ.setdefault("CLOUD_PROVIDER", "local")
    os.environ.setdefault("GCP_PROJECT_ID", "test-project")

    from alerting_service.config import AlertingSystemConfig

    cfg = AlertingSystemConfig(cloud_provider="local", gcp_project_id="test-project")
    assert cfg.service_name == "alerting-service"


@pytest.mark.unit
def test_health_endpoint_returns_200(api_client: TestClient) -> None:
    """/health must return 200."""
    response = api_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "alerting-service"


@pytest.mark.unit
def test_readiness_endpoint_returns_200_or_503(api_client: TestClient) -> None:
    """/readiness must respond (200 or 503 depending on config availability)."""
    response = api_client.get("/readiness")
    assert response.status_code in (200, 503)
    data = response.json()
    assert "status" in data


@pytest.mark.unit
def test_api_main_importable() -> None:
    """FastAPI app object must be importable."""
    from alerting_service.api.main import app

    assert app is not None
    assert app.title == "alerting-service"


@pytest.mark.unit
def test_metrics_importable() -> None:
    """Prometheus metrics module must be importable."""
    from alerting_service.metrics import PROCESSING_LATENCY, RECORDS_PROCESSED

    assert RECORDS_PROCESSED is not None
    assert PROCESSING_LATENCY is not None


@pytest.mark.unit
def test_alert_subscriber_uses_queue_client() -> None:
    """AlertSubscriber must use get_queue_client — no direct google-cloud-pubsub."""
    mock_client = MagicMock()
    with patch(
        "alerting_service.subscribers.alert_subscriber.get_queue_client",
        return_value=mock_client,
    ):
        from alerting_service.subscribers.alert_subscriber import AlertSubscriber

        subscriber = AlertSubscriber(project_id="test-project")
        assert subscriber._client is mock_client


@pytest.mark.unit
def test_no_direct_google_pubsub_import() -> None:
    """Confirm alert_subscriber does not import google.cloud.pubsub_v1 directly."""
    import pathlib
    import re

    src = pathlib.Path("alerting_service/subscribers/alert_subscriber.py")
    content = src.read_text()
    # Match actual import statements, not docstring comments
    has_direct_import = bool(re.search(r"^\s*(import|from)\s+google\.cloud\.pubsub", content, re.MULTILINE))
    assert not has_direct_import, (
        "alert_subscriber.py must not import google-cloud-pubsub directly; "
        "use get_queue_client from unified_trading_library.cloud_interface"
    )


@pytest.mark.unit
def test_alert_store_importable() -> None:
    """Alert store must be importable."""
    from alerting_service.core.alert_store import AlertEvent, AlertStore

    store = AlertStore()
    assert store is not None
    assert AlertEvent is not None


@pytest.mark.unit
async def test_alert_subscriber_stream_processes_messages() -> None:
    """AlertSubscriber.stream() must process a message batch."""
    import json

    from alerting_service.subscribers.alert_subscriber import AlertSubscriber

    payload = json.dumps({"event_name": "CIRCUIT_BREAKER_OPEN", "details": "test"}).encode()
    mock_client = MagicMock()
    mock_client.subscribe_once = MagicMock(return_value=[(payload, {"source": "test_sub"})])

    received: list[tuple[str, dict[str, object]]] = []

    with (
        patch(
            "alerting_service.subscribers.alert_subscriber.get_queue_client",
            return_value=mock_client,
        ),
        patch("alerting_service.subscribers.alert_subscriber.route_event"),
    ):
        subscriber = AlertSubscriber(
            project_id="test-project",
            subscriptions=("test_sub",),
            poll_interval_seconds=0,
        )
        # Only pull one message then stop
        subscriber._running = True

        async def _one_shot() -> None:
            async for event_name, details in subscriber.stream():
                received.append((event_name, details))
                subscriber.stop()

        import asyncio

        await asyncio.wait_for(_one_shot(), timeout=3.0)

    assert len(received) == 1
    assert received[0][0] == "CIRCUIT_BREAKER_OPEN"
