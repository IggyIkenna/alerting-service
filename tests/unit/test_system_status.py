"""Unit tests for system_health_aggregator and /system-status route."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from alerting_service.config import AlertingSystemConfig
from alerting_service.core.system_health_aggregator import (
    _DEFAULT_SERVICE_URLS,
    _build_service_urls,
    _parse_health_response,
    get_system_health,
)


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    """Reset module-level cache between tests."""
    import alerting_service.core.system_health_aggregator as mod

    mod._cache = {}
    mod._cache_time = 0.0


_TWO_SERVICES: dict[str, str] = {
    "svc-a": "http://svc-a:8001",
    "svc-b": "http://svc-b:8002",
}


def _inject_urls(cfg: AlertingSystemConfig) -> dict[str, str]:
    return dict(_TWO_SERVICES)


def _mock_resp(payload: dict[str, object]) -> MagicMock:
    """Build an httpx.Response mock whose .content is JSON bytes."""
    resp = MagicMock()
    resp.content = json.dumps(payload).encode()
    return resp


class TestBuildServiceUrls:
    def test_returns_defaults_when_metrics_endpoints_empty(self) -> None:
        cfg = AlertingSystemConfig()
        # metrics_endpoints is a ClassVar defaulting to {} — do not mutate
        urls = _build_service_urls(cfg)
        assert urls == _DEFAULT_SERVICE_URLS

    def test_returns_class_level_endpoints_when_populated(self) -> None:
        custom = {"my-service": "http://custom:9000"}
        original = AlertingSystemConfig.metrics_endpoints
        try:
            AlertingSystemConfig.metrics_endpoints = custom  # type: ignore[assignment]
            cfg = AlertingSystemConfig()
            urls = _build_service_urls(cfg)
            assert urls == custom
        finally:
            AlertingSystemConfig.metrics_endpoints = original  # type: ignore[assignment]


class TestParseHealthResponse:
    def test_parses_status_and_checks(self) -> None:
        resp = _mock_resp({"status": "ok", "checks": {"db": "ok"}})
        status, checks = _parse_health_response(resp)
        assert status == "ok"
        assert checks == {"db": "ok"}

    def test_returns_unknown_for_empty_body(self) -> None:
        resp = _mock_resp({})
        status, checks = _parse_health_response(resp)
        assert status == "unknown"
        assert checks == {}

    def test_checks_defaults_to_empty_dict_when_missing(self) -> None:
        resp = _mock_resp({"status": "ok"})
        status, checks = _parse_health_response(resp)
        assert status == "ok"
        assert checks == {}


class TestGetSystemHealth:
    def test_returns_ok_when_all_services_healthy(self) -> None:
        cfg = AlertingSystemConfig()
        mock_resp = _mock_resp({"status": "ok", "checks": {"db": "ok"}})

        with patch("alerting_service.core.system_health_aggregator.httpx.get", return_value=mock_resp):
            result = get_system_health(cfg, get_service_urls=_inject_urls)

        assert result["overall"] == "ok"
        services = result["services"]
        assert isinstance(services, dict)
        assert services["svc-a"]["status"] == "ok"  # type: ignore[index]
        assert services["svc-b"]["status"] == "ok"  # type: ignore[index]

    def test_overall_degraded_when_service_unreachable(self) -> None:
        cfg = AlertingSystemConfig()

        import httpx as httpx_mod

        def side_effect(url: str, timeout: float) -> MagicMock:
            if "svc-a" in url:
                raise httpx_mod.ConnectError("refused")
            return _mock_resp({"status": "ok"})

        with patch("alerting_service.core.system_health_aggregator.httpx.get", side_effect=side_effect):
            result = get_system_health(cfg, get_service_urls=_inject_urls)

        assert result["overall"] == "degraded"
        services = result["services"]
        assert isinstance(services, dict)
        assert services["svc-a"]["status"] == "unreachable"  # type: ignore[index]

    def test_overall_degraded_when_service_unhealthy(self) -> None:
        cfg = AlertingSystemConfig()

        with patch(
            "alerting_service.core.system_health_aggregator.httpx.get",
            return_value=_mock_resp({"status": "unhealthy"}),
        ):
            result = get_system_health(cfg, get_service_urls=_inject_urls)

        assert result["overall"] == "degraded"

    def test_cache_returns_cached_result_without_hitting_network(self) -> None:
        import alerting_service.core.system_health_aggregator as mod

        cfg = AlertingSystemConfig()
        cached: dict[str, object] = {"services": {}, "overall": "ok"}
        mod._cache = cached
        mod._cache_time = 1e15  # far in the future — cache never expires

        call_count = 0

        def counting_get(url: str, timeout: float) -> MagicMock:
            nonlocal call_count
            call_count += 1
            return _mock_resp({"status": "ok"})

        with patch("alerting_service.core.system_health_aggregator.httpx.get", side_effect=counting_get):
            result = get_system_health(cfg, get_service_urls=_inject_urls)

        assert call_count == 0
        assert result is cached

    def test_custom_get_service_urls_overrides_default(self) -> None:
        cfg = AlertingSystemConfig()
        custom_urls = {"custom-svc": "http://custom:9999"}

        with patch(
            "alerting_service.core.system_health_aggregator.httpx.get",
            return_value=_mock_resp({"status": "ok"}),
        ):
            result = get_system_health(cfg, get_service_urls=lambda _: custom_urls)

        services = result["services"]
        assert isinstance(services, dict)
        assert "custom-svc" in services
        assert "svc-a" not in services

    def test_result_is_cached_after_first_call(self) -> None:
        cfg = AlertingSystemConfig()

        with patch(
            "alerting_service.core.system_health_aggregator.httpx.get",
            return_value=_mock_resp({"status": "ok"}),
        ):
            first = get_system_health(cfg, get_service_urls=_inject_urls)

        # Second call with network fully patched out should still hit cache
        with patch(
            "alerting_service.core.system_health_aggregator.httpx.get",
            side_effect=RuntimeError("should not call network"),
        ):
            second = get_system_health(cfg, get_service_urls=_inject_urls)

        assert first is second


class TestSystemStatusRoute:
    def test_route_returns_dict_with_overall_and_services(self) -> None:
        cfg = AlertingSystemConfig()

        with patch("alerting_service.api.routes.system_status.AlertingSystemConfig", return_value=cfg):
            original_fn = __import__(
                "alerting_service.core.system_health_aggregator", fromlist=["get_system_health"]
            ).get_system_health

            def patched_health(c: AlertingSystemConfig) -> dict[str, object]:
                with patch(
                    "alerting_service.core.system_health_aggregator.httpx.get",
                    return_value=_mock_resp({"status": "ok"}),
                ):
                    return original_fn(c, get_service_urls=_inject_urls)

            with patch("alerting_service.api.routes.system_status.get_system_health", patched_health):
                from alerting_service.api.routes.system_status import system_status

                result = system_status()

        assert "overall" in result
        assert "services" in result
