"""
PagerDuty notifier using the Events API v2.

Reads the routing key from Secret Manager and sends trigger events
via a synchronous httpx POST to https://events.pagerduty.com/v2/enqueue.
"""

import logging
from functools import lru_cache
from typing import Literal

import httpx
from unified_trading_library import SecretClient, get_secret_client
from unified_trading_library import UnifiedCloudConfig
from unified_trading_library import log_event
from unified_trading_library.core.fault_injection import get_fault_transport

logger = logging.getLogger(__name__)

_PAGERDUTY_ENQUEUE_URL = "https://events.pagerduty.com/v2/enqueue"
_SECRET_NAME = "alerting-pagerduty-routing-key"

PagerDutySeverity = Literal["critical", "error", "warning", "info"]


@lru_cache(maxsize=1)
def _get_cloud_config() -> UnifiedCloudConfig:
    """Return singleton UnifiedCloudConfig instance."""
    return UnifiedCloudConfig()


def _get_routing_key(project_id: str) -> str:
    """Retrieve the PagerDuty routing key from Secret Manager."""
    client: SecretClient = get_secret_client(project_id=project_id)
    secret_value: str | None = client.get_secret(_SECRET_NAME)
    if secret_value is None:
        raise RuntimeError(f"Secret '{_SECRET_NAME}' not found in Secret Manager")
    return secret_value


def send_event(
    summary: str,
    severity: PagerDutySeverity,
    source: str,
    details: dict[str, object],
) -> bool:
    """Send a trigger event to PagerDuty Events API v2.

    Args:
        summary: Human-readable summary of the event.
        severity: One of "critical", "error", "warning", "info".
        source: Logical name of the component that emitted the event.
        details: Arbitrary additional context included in the event body.

    Returns:
        True when PagerDuty accepted the event (HTTP 202), False otherwise.
    """
    config = _get_cloud_config()
    project_id = config.gcp_project_id

    routing_key = _get_routing_key(project_id)

    payload: dict[str, object] = {
        "routing_key": routing_key,
        "event_action": "trigger",
        "payload": {
            "summary": summary,
            "severity": severity,
            "source": source,
            "custom_details": details,
        },
    }

    try:
        fault_transport = get_fault_transport()
        if fault_transport is not None:
            with httpx.Client(transport=fault_transport, timeout=10.0) as client:
                response = client.post(_PAGERDUTY_ENQUEUE_URL, json=payload)
        else:
            response = httpx.post(_PAGERDUTY_ENQUEUE_URL, json=payload, timeout=10.0)
        if response.status_code == 202:
            log_event(
                "PAGERDUTY_EVENT_SENT",
                details={"summary": summary, "severity": severity, "source": source},
            )
            return True
        logger.error(
            "PagerDuty returned unexpected status %d: %s",
            response.status_code,
            response.text,
        )
        return False
    except httpx.HTTPError as exc:
        logger.error("Failed to send PagerDuty event: %s", exc)
        return False
