"""Unit tests for alerting_service.api.routes.health and alerting_service.auth."""

from __future__ import annotations

import pytest
from fastapi import HTTPException


class TestHealthEndpoint:
    def test_health_returns_ok(self) -> None:
        import asyncio

        from alerting_service.api.routes.health import health

        result = asyncio.get_event_loop().run_until_complete(health())
        assert result["status"] == "ok"
        assert result["service"] == "alerting-system"
        assert "cloud_provider" in result
        assert "mock_mode" in result

    def test_health_data_freshness_is_dict(self) -> None:
        import asyncio

        from alerting_service.api.routes.health import health

        result = asyncio.get_event_loop().run_until_complete(health())
        freshness = result.get("data_freshness")
        assert isinstance(freshness, dict), f"data_freshness must be dict, got {type(freshness)}"

    def test_health_data_freshness_fields(self) -> None:
        import asyncio

        from alerting_service.api.routes.health import health

        result = asyncio.get_event_loop().run_until_complete(health())
        freshness = result["data_freshness"]
        assert isinstance(freshness.get("last_processed_date"), str)
        assert isinstance(freshness.get("stale"), bool)


class TestVerifyApiKey:
    @pytest.mark.asyncio
    async def test_verify_returns_dev_mode_when_auth_disabled(self) -> None:
        import alerting_service.auth as auth_module

        original = auth_module.DISABLE_AUTH
        try:
            auth_module.DISABLE_AUTH = True
            result = await auth_module.verify_api_key(api_key=None)
            assert result == "dev-mode"
        finally:
            auth_module.DISABLE_AUTH = original

    @pytest.mark.asyncio
    async def test_verify_raises_401_when_no_key_and_auth_enabled(self) -> None:
        import alerting_service.auth as auth_module

        original = auth_module.DISABLE_AUTH
        try:
            auth_module.DISABLE_AUTH = False
            with pytest.raises(HTTPException) as exc_info:
                await auth_module.verify_api_key(api_key=None)
            assert exc_info.value.status_code == 401
        finally:
            auth_module.DISABLE_AUTH = original

    @pytest.mark.asyncio
    async def test_verify_raises_401_when_wrong_key(self) -> None:
        import alerting_service.auth as auth_module

        original_disable = auth_module.DISABLE_AUTH
        original_api_key = auth_module._auth_cfg.api_key
        try:
            auth_module.DISABLE_AUTH = False
            auth_module._auth_cfg.api_key = "correct-key"
            with pytest.raises(HTTPException) as exc_info:
                await auth_module.verify_api_key(api_key="wrong-key")
            assert exc_info.value.status_code == 401
        finally:
            auth_module.DISABLE_AUTH = original_disable
            auth_module._auth_cfg.api_key = original_api_key

    @pytest.mark.asyncio
    async def test_verify_returns_key_when_correct(self) -> None:
        import alerting_service.auth as auth_module

        original_disable = auth_module.DISABLE_AUTH
        original_api_key = auth_module._auth_cfg.api_key
        try:
            auth_module.DISABLE_AUTH = False
            auth_module._auth_cfg.api_key = "my-secret-key"
            result = await auth_module.verify_api_key(api_key="my-secret-key")
            assert result == "my-secret-key"
        finally:
            auth_module.DISABLE_AUTH = original_disable
            auth_module._auth_cfg.api_key = original_api_key
