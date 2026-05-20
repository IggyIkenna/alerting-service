"""System health aggregator — polls all service /health endpoints with TTL cache."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import cast

import httpx
from unified_trading_library import get_fault_transport

from alerting_service.config import AlertingSystemConfig

_cache: dict[str, object] = {}
_cache_time: float = 0.0
_TTL_SECONDS: float = 30.0

# Default service base URLs (overridden by config.metrics_endpoints if populated)
_DEFAULT_SERVICE_URLS: dict[str, str] = {
    "market-data-api": "http://localhost:8001",
    "execution-results-api": "http://localhost:8002",
    "client-reporting-api": "http://localhost:8003",
    "deployment-api": "http://localhost:8004",
    "execution-service": "http://localhost:8005",
    "strategy-service": "http://localhost:8006",
}


def _build_service_urls(cfg: AlertingSystemConfig) -> dict[str, str]:
    """Return mapping of service_name -> base_url from config.

    Uses config.metrics_endpoints when populated; falls back to defaults.
    """
    if cfg.metrics_endpoints:
        return dict(cfg.metrics_endpoints)
    return dict(_DEFAULT_SERVICE_URLS)


def _parse_health_response(resp: httpx.Response) -> tuple[str, dict[str, object]]:
    """Parse a /health response bytes and return (status, checks).

    Uses json.loads + cast so basedpyright sees a typed dict, not Any.
    """
    data: dict[str, object] = cast(dict[str, object], json.loads(resp.content))
    if not data:
        return "unknown", {}
    status_val = data.get("status", "unknown")
    status = str(status_val) if status_val is not None else "unknown"
    checks_val = data.get("checks")
    checks: dict[str, object] = (
        cast(dict[str, object], checks_val) if isinstance(checks_val, dict) else {}
    )
    return status, checks


def get_system_health(
    cfg: AlertingSystemConfig,
    *,
    get_service_urls: Callable[[AlertingSystemConfig], dict[str, str]] | None = None,
) -> dict[str, object]:
    """Return aggregated health for all services with 30s TTL cache."""
    global _cache, _cache_time

    now = time.monotonic()
    if _cache and (now - _cache_time) < _TTL_SECONDS:
        return _cache

    service_urls = (get_service_urls or _build_service_urls)(cfg)
    services: dict[str, object] = {}
    overall = "ok"

    fault_transport = get_fault_transport()

    for name, base_url in service_urls.items():
        try:
            if fault_transport is not None:
                with httpx.Client(transport=fault_transport, timeout=3.0) as client:
                    resp = client.get(f"{base_url}/health")
            else:
                resp = httpx.get(f"{base_url}/health", timeout=3.0)
            status, checks = _parse_health_response(resp)
        except (httpx.HTTPError, httpx.TimeoutException, ValueError, OSError):
            status = "unreachable"
            checks = {}
        services[name] = {"status": status, "checks": checks}
        if status in ("unhealthy", "unreachable") and overall == "ok":
            overall = "degraded"

    result: dict[str, object] = {"services": services, "overall": overall}
    _cache = result
    _cache_time = now
    return result
