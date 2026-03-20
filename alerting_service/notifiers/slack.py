"""
Slack notifier using Incoming Webhooks.

Reads the webhook URL from Secret Manager and posts messages via a
synchronous httpx POST to the webhook endpoint.
"""

import logging
from functools import lru_cache

import httpx
from unified_cloud_interface import SecretClient, get_secret_client
from unified_config_interface import UnifiedCloudConfig
from unified_events_interface import log_event
from unified_trading_library.core.fault_injection import get_fault_transport

logger = logging.getLogger(__name__)

_SECRET_NAME = "alerting-slack-webhook-url"


@lru_cache(maxsize=1)
def _get_cloud_config() -> UnifiedCloudConfig:
    """Return singleton UnifiedCloudConfig instance."""
    return UnifiedCloudConfig()


def _get_webhook_url(project_id: str) -> str:
    """Retrieve the Slack webhook URL from Secret Manager."""
    client: SecretClient = get_secret_client(project_id=project_id)
    secret_value: str | None = client.get_secret(_SECRET_NAME)
    if secret_value is None:
        raise RuntimeError(f"Secret '{_SECRET_NAME}' not found in Secret Manager")
    return secret_value


def send_message(
    text: str,
    channel: str | None = None,
    blocks: list[dict[str, object]] | None = None,
) -> bool:
    """Post a message to Slack via an Incoming Webhook.

    Args:
        text: Fallback plain-text message (required by Slack even when blocks are used).
        channel: Optional override channel (e.g. "#pipeline-alerts"). Webhook default
                 channel is used when this is None.
        blocks: Optional Block Kit payload to replace the plain-text message body.

    Returns:
        True when Slack accepted the message (HTTP 200), False otherwise.
    """
    config = _get_cloud_config()
    project_id = config.gcp_project_id

    webhook_url = _get_webhook_url(project_id)

    payload: dict[str, object] = {"text": text}
    if channel is not None:
        payload["channel"] = channel
    if blocks is not None:
        payload["blocks"] = blocks

    try:
        fault_transport = get_fault_transport()
        if fault_transport is not None:
            with httpx.Client(transport=fault_transport, timeout=10.0) as client:
                response = client.post(webhook_url, json=payload)
        else:
            response = httpx.post(webhook_url, json=payload, timeout=10.0)
        if response.status_code == 200:
            log_event("SLACK_MESSAGE_SENT", details={"text": text, "channel": channel or "default"})
            return True
        logger.error(
            "Slack webhook returned unexpected status %d: %s",
            response.status_code,
            response.text,
        )
        return False
    except httpx.HTTPError as exc:
        logger.error("Failed to send Slack message: %s", exc)
        return False
