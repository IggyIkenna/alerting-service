"""API key authentication for alerting-service."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from unified_trading_library import UnifiedCloudConfig, log_event

logger = logging.getLogger(__name__)

# --- Production guard for DISABLE_AUTH ---
_auth_cfg = UnifiedCloudConfig()
auth_cfg = _auth_cfg  # public alias for use in api/main.py
_disable_auth_raw = _auth_cfg.disable_auth
_environment = _auth_cfg.environment
if _disable_auth_raw and _environment == "production":
    log_event(
        "AUTH_MISCONFIGURED",
        severity="CRITICAL",
        details={"reason": "DISABLE_AUTH_in_production", "environment": _environment},
    )
    raise RuntimeError(
        "DISABLE_AUTH=true is forbidden in production. "
        "Service refuses to start with auth disabled. "
        "Unset DISABLE_AUTH or set ENVIRONMENT != production."
    )
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
        log_event(
            "AUTH_FAILURE",
            severity="WARNING",
            details={
                "auth_type": "api_key",
                "reason": "missing_key",
                "service": "alerting-service",
            },
        )
        raise HTTPException(status_code=401, detail="Missing API key")
    expected_key = _auth_cfg.api_key
    if not expected_key or api_key != expected_key:
        log_event(
            "AUTH_FAILURE",
            severity="WARNING",
            details={
                "auth_type": "api_key",
                "reason": "invalid_key",
                "service": "alerting-service",
            },
        )
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key
