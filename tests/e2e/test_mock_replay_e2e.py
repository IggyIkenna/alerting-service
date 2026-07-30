"""Real E2E tests for alerting-service.

Replaces the previous "mock replay" harness, which never imported
``alerting_service.*`` at all — it loaded UAC VCR cassettes and re-implemented
ad-hoc healthy/unhealthy assertions over the raw cassette bodies, so it could
pass even if the service's own subscriber/rules/notifiers/persistence code was
completely broken.

This suite drives the REAL production code paths end-to-end:

1. ``AlertSubscriber.dispatch_event()`` (the same entry point ``main.py`` uses
   for batch replay) -> ``AlertSubscriber.dispatch_event`` -> the real
   ``notifiers.router.route_event`` (dedup, UAC routing-rule match, channel
   resolution) -> the real ``AlertStorageStore`` persistence.
2. Alert lifecycle (open/ack/resolve) through the real
   ``AlertStorageStore.write_alert_history`` / ``read_delivery_records``
   persistence methods.
3. The real ``/alerts/delivery-status/{alert_id}`` FastAPI route querying the
   same real persistence store after a real triggered event.
4. Multi-service health aggregation via the real
   ``core.system_health_aggregator.get_system_health`` implementation.

Only the true external-network boundary is stubbed (via ``respx``, which
intercepts at the httpx transport layer): the Slack webhook POST and the
per-service ``/health`` GETs. Everything else — dedup, rule matching, channel
resolution, JSON/JSONL serialization, GCS-path conventions, blob listing — is
the real service code running against a real on-disk
``LocalStorageProvider`` (no GCS mocking), per
``local_storage_provider_shared_tempdir_test_state_leak_2026_07_20.md``'s
guidance to always pass an explicit per-test ``base_dir``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from unified_trading_library import AuthContext
from unified_trading_library.cloud_interface.providers.local import LocalStorageProvider  # noqa: qg-deep-import

from alerting_service.api.routes import delivery_status as ds_module
from alerting_service.config import AlertingSystemConfig
from alerting_service.core import system_health_aggregator
from alerting_service.notifiers import router as router_module
from alerting_service.persistence.storage_store import AlertStorageStore
from alerting_service.subscribers.alert_subscriber import AlertSubscriber

pytestmark = pytest.mark.e2e

_FIXED_ALERT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _fake_auth() -> AuthContext:
    return AuthContext(org_id="internal", user_id="test-key", role="admin", is_internal=True, is_api_key=True)


@pytest.fixture
def local_storage_store(tmp_path: Path) -> AlertStorageStore:
    """A REAL ``AlertStorageStore`` backed by an on-disk ``LocalStorageProvider``.

    No GCS mocking: ``upload_bytes``/``download_bytes``/``list_blobs`` run
    against a real per-test temp directory, so
    ``write_alert_history()`` -> ``read_delivery_records()`` exercises the
    actual serialization + date-partition blob-listing logic.
    """
    client = LocalStorageProvider(base_dir=str(tmp_path))
    return AlertStorageStore(storage_client=client, bucket="alerting-e2e-test")


@pytest.fixture
def wired_router(local_storage_store: AlertStorageStore, monkeypatch: pytest.MonkeyPatch) -> AlertingSystemConfig:
    """Point the real router at the local storage store + a fake Slack webhook
    and disable PagerDuty (a real, supported config flag — the same one used
    for staging quietness runs) so ``route_event()`` runs its full real logic
    with only the outbound HTTP calls stubbed at the transport layer."""
    cfg = AlertingSystemConfig()
    cfg.uts_live_alerts_slack_webhook = "https://hooks.example.test/slack"
    cfg.pagerduty_disabled = True
    monkeypatch.setattr(router_module, "_get_cloud_config", lambda: cfg)
    monkeypatch.setattr(router_module, "_get_storage_store", lambda: local_storage_store)
    monkeypatch.setattr(router_module, "get_paging_credentials", lambda: {})
    monkeypatch.setattr(router_module.uuid, "uuid4", lambda: _FIXED_ALERT_ID)
    return cfg


# ---------------------------------------------------------------------------
# (1) subscriber -> rules -> notifiers pipeline on a real injected event
# ---------------------------------------------------------------------------


class TestSubscriberRulesNotifiersPipeline:
    """E2E: a real event flows subscriber -> router (UAC rules) -> notifier -> persistence."""

    @respx.mock
    def test_real_injected_event_routes_and_persists(
        self,
        wired_router: AlertingSystemConfig,
        local_storage_store: AlertStorageStore,
    ) -> None:
        slack_route = respx.post("https://hooks.example.test/slack").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        # AlertSubscriber.dispatch_event is the SAME entry point main.py's
        # batch-replay path uses (AlertSubscriber.dispatch_event(event_name,
        # enriched)) -- a real injected event, not a hand-rolled assertion.
        AlertSubscriber.dispatch_event(
            "CIRCUIT_BREAKER_OPEN",
            {"venue": "binance", "message": "Binance circuit OPEN", "source": "execution-service"},
        )

        # Real UAC routing rule for CIRCUIT_BREAKER_OPEN is pagerduty+telegram;
        # pagerduty is disabled so only the slack/telegram mirror fires.
        assert slack_route.called
        assert b"CIRCUIT_BREAKER_OPEN" in slack_route.calls[0].request.content

        # Real persistence: the router's own _persist_delivery_record wrote a
        # real JSONL blob via the real AlertStorageStore.
        alert_id = _FIXED_ALERT_ID.hex[:16]
        records = local_storage_store.read_delivery_records(alert_id)
        assert len(records) == 1
        assert records[0]["channel"] == "slack"
        assert records[0]["status"] == "sent"
        assert records[0]["event_name"] == "CIRCUIT_BREAKER_OPEN"


# ---------------------------------------------------------------------------
# (2) alert lifecycle (open/ack/resolve) via persistence
# ---------------------------------------------------------------------------


class TestAlertLifecycleViaPersistence:
    """E2E: open -> ack -> resolve transitions written + read back through the
    real AlertStorageStore (no in-memory reimplementation)."""

    def test_lifecycle_transitions_round_trip(self, local_storage_store: AlertStorageStore) -> None:
        alert_id = "lifecycle-e2e-alert"

        local_storage_store.write_alert_history(
            {"alert_id": alert_id, "status": "open", "event_name": "MARGIN_WARNING", "message": "margin low"}
        )
        local_storage_store.write_alert_history(
            {"alert_id": alert_id, "status": "acked", "event_name": "MARGIN_WARNING", "acked_by": "operator"}
        )
        local_storage_store.write_alert_history(
            {"alert_id": alert_id, "status": "resolved", "event_name": "MARGIN_WARNING", "resolved_by": "operator"}
        )

        records = local_storage_store.read_delivery_records(alert_id)
        statuses = {str(r["status"]) for r in records}
        assert statuses == {"open", "acked", "resolved"}
        assert all(r["alert_id"] == alert_id for r in records)


# ---------------------------------------------------------------------------
# (3) api/routes query after an injected trigger
# ---------------------------------------------------------------------------


class TestDeliveryStatusApiAfterInjectedTrigger:
    """E2E: the real FastAPI delivery-status route reads what the real router
    just persisted for a real injected event."""

    @pytest.fixture
    def client(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
        from alerting_service.api.main import _api_auth, app

        monkeypatch.setattr(ds_module._cfg, "data_mode", "real")
        monkeypatch.setattr(ds_module._cfg, "cloud_mock_mode", False)
        app.dependency_overrides[_api_auth] = _fake_auth
        yield TestClient(app)
        app.dependency_overrides.clear()

    @respx.mock
    def test_query_returns_records_from_real_trigger(
        self,
        client: TestClient,
        wired_router: AlertingSystemConfig,
        local_storage_store: AlertStorageStore,
    ) -> None:
        from alerting_service.api.main import app
        from alerting_service.api.routes.delivery_status import get_delivery_store

        app.dependency_overrides[get_delivery_store] = lambda: local_storage_store

        respx.post("https://hooks.example.test/slack").mock(return_value=httpx.Response(200, json={"ok": True}))
        AlertSubscriber.dispatch_event(
            "CIRCUIT_BREAKER_OPEN",
            {"venue": "deribit", "message": "Deribit circuit OPEN", "source": "execution-service"},
        )
        alert_id = _FIXED_ALERT_ID.hex[:16]

        response = client.get(f"/alerts/delivery-status/{alert_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["alert_id"] == alert_id
        assert len(data["records"]) == 1
        assert data["records"][0]["channel"] == "slack"
        assert data["records"][0]["event_name"] == "CIRCUIT_BREAKER_OPEN"


# ---------------------------------------------------------------------------
# (4) multi-venue aggregation via the real engine code
# ---------------------------------------------------------------------------


class TestMultiServiceHealthAggregation:
    """E2E: real ``core.system_health_aggregator.get_system_health`` aggregates
    per-venue/service health -- not an inline reimplementation of the
    healthy/unhealthy decision."""

    @respx.mock
    def test_one_unhealthy_venue_degrades_overall_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Reset the module-level TTL cache so this test observes a fresh real
        # aggregation call rather than another test's cached result.
        monkeypatch.setattr(system_health_aggregator, "_cache", {})
        monkeypatch.setattr(system_health_aggregator, "_cache_time", 0.0)
        monkeypatch.setattr(system_health_aggregator, "get_fault_transport", lambda: None)

        respx.get("http://binance-md.test/health").mock(
            return_value=httpx.Response(200, json={"status": "healthy", "checks": {}})
        )
        respx.get("http://deribit-md.test/health").mock(
            return_value=httpx.Response(200, json={"status": "unhealthy", "checks": {"pubsub": "down"}})
        )

        cfg = MagicMock()
        cfg.metrics_endpoints = {}

        result = system_health_aggregator.get_system_health(
            cfg,
            get_service_urls=lambda _cfg: {
                "binance-md": "http://binance-md.test",
                "deribit-md": "http://deribit-md.test",
            },
        )

        assert result["overall"] == "degraded"
        services = cast(dict[str, object], result["services"])
        assert cast(dict[str, object], services["binance-md"])["status"] == "healthy"
        assert cast(dict[str, object], services["deribit-md"])["status"] == "unhealthy"
