"""
PagerDuty notifier using the Events API v2.

Reads the routing key from Secret Manager and sends trigger events
via a synchronous httpx POST to https://events.pagerduty.com/v2/enqueue.

Capability-probe pattern (2026-08-06 fix — PagerDuty was never provisioned,
so ``alerting-pagerduty-routing-key`` genuinely does not exist in Secret
Manager yet): the routing-key lookup used to RAISE ``RuntimeError`` on a
missing secret, unguarded at every ``send_event()`` call — every recurring
alert (e.g. CONSOLIDATOR_DOWN re-firing every ~60s) independently crashed
through ``alert_subscriber._route_one``'s generic except, paging
``ALERT_DISPATCH_FAILED`` for 10+ hours straight instead of the real
incident. ``_probe_routing_key`` now probes Secret Manager ONCE (cached for
the process lifetime, mirroring the ``_ACTUATORS_AVAILABLE =
importlib.util.find_spec(...)`` capability-probe idiom used elsewhere in this
workspace, e.g. deployment-service's ``escalation.py``) and ``send_event``
degrades to ``return False`` (logged once, not per-call) instead of raising —
the router's CRITICAL-severity email fallback (``notifiers/email.py``) then
takes over so a CRITICAL incident is never fully silent.
"""

import logging
from functools import lru_cache
from typing import Literal

import httpx
from unified_trading_library import SecretClient, UnifiedCloudConfig, get_secret_client, log_event

logger = logging.getLogger(__name__)

_PAGERDUTY_ENQUEUE_URL = "https://events.pagerduty.com/v2/enqueue"
_SECRET_NAME = "alerting-pagerduty-routing-key"

PagerDutySeverity = Literal["critical", "error", "warning", "info"]


@lru_cache(maxsize=1)
def _get_cloud_config() -> UnifiedCloudConfig:
    """Return singleton UnifiedCloudConfig instance."""
    return UnifiedCloudConfig()


@lru_cache(maxsize=1)
def _probe_routing_key(project_id: str) -> str | None:
    """Probe Secret Manager ONCE for the PagerDuty routing key; cache the
    result for the process lifetime (capability-probe pattern — probe once,
    don't crash per-call).

    Returns ``None`` (and logs a single WARNING) when the secret is absent OR
    the Secret Manager call itself fails (permission / network / malformed
    name) — either way PagerDuty is simply unavailable, never a raised
    exception a caller has to guard against. Cached via ``lru_cache``, so a
    missing secret is logged exactly once per process, not once per alert
    fire; provisioning the secret later takes effect on the next deploy
    (process restart), matching the reference capability-probe pattern.
    """
    try:
        client: SecretClient = get_secret_client(project_id=project_id)
        secret_value: str | None = client.get_secret(_SECRET_NAME)
    except Exception as exc:  # capability probe — SM errors must never propagate
        logger.warning(
            "PagerDuty routing key probe failed (secret=%s): %s — PagerDuty marked unavailable",
            _SECRET_NAME,
            exc,
        )
        return None
    if secret_value is None:
        logger.warning(
            "PagerDuty routing key secret '%s' not found in Secret Manager — PagerDuty marked "
            "unavailable (CRITICAL events fall back to email; see notifiers/email.py)",
            _SECRET_NAME,
        )
        return None
    return secret_value


def is_available(project_id: str | None = None) -> bool:
    """Return True iff PagerDuty has a usable routing key (probed once, cached).

    ``project_id=None`` resolves via the singleton ``UnifiedCloudConfig``
    (mirrors ``send_event``'s own resolution). Exposed for health-checks /
    tests; ``send_event`` does not call this separately — it consults the
    same cached probe directly.
    """
    pid = project_id or _get_cloud_config().gcp_project_id
    return _probe_routing_key(pid) is not None


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
        True when PagerDuty accepted the event (HTTP 202), False otherwise
        (including when PagerDuty is unavailable — no routing key probed —
        which is a normal degrade, never a raised exception).
    """
    config = _get_cloud_config()
    project_id = config.gcp_project_id

    routing_key = _probe_routing_key(project_id)
    if routing_key is None:
        return False

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
