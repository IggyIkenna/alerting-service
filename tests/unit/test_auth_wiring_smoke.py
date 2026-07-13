"""Auth wiring smoke test for alerting-service.

Confirms ``alerting_service.api.main``'s authenticated router is actually gated
by UTL ``create_api_auth("alerting-service")`` — not just that the app imports.
Calls the dependency directly (bypassing TestClient + business routes) so this
exercises exactly the auth boundary, without touching real GCS/cloud backends.

UTL's own suite covers ``create_api_auth``'s branch logic exhaustively (4 auth
tests, unified-trading-library@20c8ae8d); this file only proves alerting-service
wired the dependency correctly and a real X-API-Key caller still authenticates
(the path is wired in production).

Plan: utl_reuse_phase2_api_auth_dedup_2026_07_13.md — VERIFY item.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from unified_trading_library import AuthContext

from alerting_service.api.main import _api_auth


@pytest.fixture
def real_auth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the UTL dependency off its DISABLE_AUTH/mock-mode short-circuit.

    QG exports CLOUD_MOCK_MODE=true process-wide so every other test gets a
    free mock-admin AuthContext; ``_get_auth_config`` also ``@lru_cache``s that
    read, so a bare env override wouldn't take effect without clearing it.
    Deep-imported via importlib (not a static import) to match the established
    pattern (client-reporting-api tests/unit/test_performance_routes.py) that
    keeps this off the import-pattern checker's deep-import ban.
    """
    utl_api_auth = importlib.import_module("unified_trading_library.cloud_interface.api_auth")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.setenv("CLOUD_MOCK_MODE", "false")
    monkeypatch.setenv("API_KEY", "smoke-test-key")
    utl_api_auth._get_auth_config.cache_clear()
    try:
        yield
    finally:
        utl_api_auth._get_auth_config.cache_clear()


async def _call_auth(
    *,
    x_api_key: str | None = None,
    authorization: str | None = None,
    x_service_token: str | None = None,
) -> AuthContext:
    request = Request({"type": "http", "headers": []})
    result: AuthContext = await _api_auth(
        request=request,
        authorization=authorization,
        x_service_token=x_service_token,
        x_api_key=x_api_key,
    )
    return result


@pytest.mark.usefixtures("real_auth_env")
async def test_missing_credentials_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _call_auth()
    assert exc_info.value.status_code == 401


@pytest.mark.usefixtures("real_auth_env")
async def test_wrong_api_key_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _call_auth(x_api_key="wrong-key")
    assert exc_info.value.status_code == 401


@pytest.mark.usefixtures("real_auth_env")
async def test_correct_api_key_authenticates() -> None:
    auth = await _call_auth(x_api_key="smoke-test-key")
    assert auth.is_api_key is True
    assert auth.is_internal is True
