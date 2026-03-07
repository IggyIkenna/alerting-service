"""API key authentication for alerting-service."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from unified_config_interface import UnifiedCloudConfig

logger = logging.getLogger(__name__)

# --- Production guard for DISABLE_AUTH ---
_auth_cfg = UnifiedCloudConfig()
_disable_auth_raw = _auth_cfg.disable_auth
_environment = _auth_cfg.environment
if _disable_auth_raw and _environment == "production":
    logging.getLogger(__name__).critical(
        "DISABLE_AUTH=true is forbidden in production. Auth remains ENABLED."
    )
    _disable_auth_raw = False
DISABLE_AUTH: bool = _disable_auth_raw

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    api_key: str | None = Security(api_key_header),
) -> str:
    """Validate the X-API-Key header against the API_KEY env var.

    Set DISABLE_AUTH=true for local development (defaults to false).
    """
    if DISABLE_AUTH:
        return "dev-mode"
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key")
    expected_key = _auth_cfg.api_key
    if not expected_key or api_key != expected_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key
