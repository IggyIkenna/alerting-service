"""
Event router: directs events to the appropriate notifier(s).

Routing is config-driven via ``AlertingSystemConfig.routing_rules``.
Each rule specifies an ``event_pattern`` (fnmatch glob), a list of
``channels``, and an optional ``severity_filter``.

Transport (2026-06-23): Slack (#uts-live-alerts) is the PRIMARY delivery for
generic/incident alerts — Telegram is RETIRED. PagerDuty stays code-wired but is
held off via PAGERDUTY_DISABLED; each Slack alert carries a "PagerDuty escalation"
SHADOW annotation (how it WOULD page) for calibration before PD is re-enabled.

Default rules (when no custom config is provided):
- KILL_SWITCH_*          -> PagerDuty (critical, shadow) + Slack #uts-live-alerts
- CIRCUIT_BREAKER_OPEN   -> PagerDuty (critical, shadow) + Slack #uts-live-alerts
- PREFLIGHT_FAILED       -> Slack #uts-live-alerts
- SERVICE_DEGRADED       -> Slack #uts-live-alerts
- All other runtime events -> Slack #uts-live-alerts (no-match catch-all)

Service event taxonomy (for observability and test compliance):
  SERVICE_EVENT: ALERT_SENT
  SERVICE_EVENT: ALERT_FAILED
  SERVICE_EVENT: KILL_SWITCH_ACTIVATED
  SERVICE_EVENT: KILL_SWITCH_DEACTIVATED
  SERVICE_EVENT: CIRCUIT_BREAKER_OPEN
  SERVICE_EVENT: PREFLIGHT_FAILED
"""

import logging
import time
import uuid
from datetime import UTC, datetime
from fnmatch import fnmatch
from functools import lru_cache
from typing import cast, get_args

from unified_api_contracts import LIVE_ALERT_RULES, AlertChannel, AlertSeverity
from unified_api_contracts.incident import (
    ImmediateSev0Override,
    IncidentEnvelope,
    IncidentState,
)
from unified_trading_library import (
    classify_and_emit_error,
    log_event,
)

from ..config import AlertingSystemConfig
from ..config_reloaders import get_paging_credentials
from ..core.dedup import AlertDeduplicator
from ..gateway.envelope_adapter import wrap_legacy_alert
from ..gateway.recovery_verifier import RecoveryVerifier
from ..gateway.state_machine import IncidentStateMachine
from ..persistence.storage_store import AlertStorageStore
from .data_pipeline_slack import send_data_pipeline_alert
from .email import send_critical_fallback
from .incident_fallback import route_incident_envelope_to_fallbacks
from .pagerduty import PagerDutySeverity
from .pagerduty import send_event as pd_send_event
from .uts_live_alerts_slack import send_uts_live_alert

logger = logging.getLogger(__name__)

_VALID_SEVERITIES: frozenset[str] = frozenset(get_args(PagerDutySeverity))

# Module-level deduplicator (shared across all route_event calls).
_deduplicator = AlertDeduplicator(ttl_seconds=60.0)

# Recurring-alert cooldowns: a sweep/breaker re-detects the SAME ongoing
# condition every tick; window >= detector cadence. The volatile-field-
# excluding dedup key collapses identity+event to ONE key held for this
# cooldown — pings once per window (re-nagging, not silence), re-alerts
# sooner on resolve+recur. CONSOLIDATOR_DOWN/MANIFEST_CONSOLIDATION_FAILED/
# FEED_REFETCH_FAILED dispatch CRITICAL via route_event_with_explicit_channels
# (2026-08-06: that path now honours this map too, was bare 60s) — hourly
# re-remind while down; each fires again on its own RESOLVED/RECOVERED name.
_RECURRING_ALERT_COOLDOWNS: dict[str, float] = {
    "DP_VM_STALL": 1800.0,  # 30 min; WARN, ~5 min sweep cadence
    "DP_EVENT_LOOP_STARVED": 1800.0,  # 30 min; WARN, ~5 min sweep cadence
    "DP_RUN_MOSTLY_EMPTY": 1800.0,  # 30 min; CRITICAL, static manifest-cell signal, >= 900s meta-sweep cadence
    "DP_VM_GONE_NO_CAPTURE": 1800.0,  # 30 min; CRITICAL, static exit-code-sweep signal, >= 300s detector cadence
    "CONSOLIDATOR_DOWN": 3600.0,  # 1h; CRITICAL, fires once + hourly re-remind while down
    "MANIFEST_CONSOLIDATION_FAILED": 3600.0,  # 1h; escalates WARN->CRITICAL on breaker-open (crash-loop)
    "FEED_REFETCH_FAILED": 3600.0,  # 1h; escalates WARN/HIGH->CRITICAL on breaker-open (same pattern)
}

# DP_RUN_MOSTLY_EMPTY STATIC BACKLOG paging-cadence downgrade lives in
# ``dp_run_mostly_empty_static_backlog.py`` (split out rather than grown here —
# router.py is already at its 1100-line file-size cap; mirrors why
# ``coalesce.py`` / ``kill_switch_rules.py`` exist as siblings below). Re-bound
# under the original private names so the test surface
# (router._dedup_window_for / router._effective_dp_severity) is unchanged.
from alerting_service.notifiers.dp_run_mostly_empty_static_backlog import (
    dedup_window_override as _dp_run_mostly_empty_dedup_window_override,
)
from alerting_service.notifiers.dp_run_mostly_empty_static_backlog import (
    effective_severity as _effective_dp_severity,
)


def _dedup_window_for(event_name: str, details: dict[str, object] | None = None) -> float | None:
    """Per-event dedup window: a cooldown for recurring alerts (WARN floods and
    opted-in static/CRITICAL conditions), else ``None`` (the deduplicator's 60s
    default). DP_RUN_MOSTLY_EMPTY widens to a daily cooldown once STATIC
    BACKLOG fires — see ``dp_run_mostly_empty_static_backlog.py``."""
    return _dp_run_mostly_empty_dedup_window_override(event_name, details, _RECURRING_ALERT_COOLDOWNS.get(event_name))


# Coalesce-window + synthetic-event suppression live in ``coalesce.py``
# (split 2026-06-12, codex ratchet plan Phase 1.5 — file-size <900).
# Re-bound here under the original private names so the test surface
# (router._is_synthetic / router._check_coalesce_window / …) and in-repo
# consumers resolve unchanged.
from alerting_service.notifiers.coalesce import (
    COALESCE_WINDOW_SECONDS as _COALESCE_WINDOW_SECONDS,
)
from alerting_service.notifiers.coalesce import (  # noqa: F401 — test-surface re-export (router._COALESCED_EVENT_NAMES)
    COALESCED_EVENT_NAMES as _COALESCED_EVENT_NAMES,
)
from alerting_service.notifiers.coalesce import (
    check_coalesce_window as _check_coalesce_window,
)
from alerting_service.notifiers.coalesce import (  # noqa: F401 — test-surface re-export
    coalesce_key as _coalesce_key,
)
from alerting_service.notifiers.coalesce import (
    is_synthetic as _is_synthetic,
)
from alerting_service.notifiers.coalesce import (  # noqa: F401 — test-surface re-export
    reset_coalesce_window_for_tests as _reset_coalesce_window_for_tests,
)

# Module-level GCS store (lazily initialised).
_storage_store_instance: object | None = None

# Batch mode flag — when True, route_event() writes audit records
# instead of delivering to PagerDuty/Telegram/Slack.
# Set from main.py before batch replay starts.
_batch_mode: bool = False

# Batch replay stats — accumulated by route_event() in batch mode.
_batch_would_deliver: dict[str, int] = {}
_batch_deduplicated: int = 0
_batch_matched: int = 0


def set_batch_mode(enabled: bool) -> None:
    """Enable or disable batch delivery suppression."""
    global _batch_mode, _batch_would_deliver, _batch_deduplicated, _batch_matched
    _batch_mode = enabled
    _batch_would_deliver = {}
    _batch_deduplicated = 0
    _batch_matched = 0


def get_batch_stats() -> dict[str, object]:
    """Return accumulated batch replay routing stats."""
    return {
        "would_deliver": dict(_batch_would_deliver),
        "deduplicated": _batch_deduplicated,
        "matched": _batch_matched,
    }


@lru_cache(maxsize=1)
def _get_cloud_config() -> AlertingSystemConfig:
    """Return singleton AlertingSystemConfig instance."""
    return AlertingSystemConfig()


def _get_storage_store() -> AlertStorageStore:
    """Return singleton AlertStorageStore instance."""
    global _storage_store_instance
    if _storage_store_instance is None:
        _storage_store_instance = AlertStorageStore()
    return cast("AlertStorageStore", _storage_store_instance)


def _parse_rule_channels(raw_channels: object) -> set[str]:
    """Parse a routing rule's ``channels`` value into a channel-name set.

    A matched rule with an empty channel list is a LOG_ONLY rule —
    ``AlertRule.to_routing_dict()`` strips ``AlertChannel.LOG_ONLY`` (UAC has no
    legacy "log_only" routing concept), leaving ``[]``. We return the sentinel
    ``{"log_only"}`` so the downstream ``_deliver_to_channels`` distinguishes
    "rule matched → INFO, no delivery" from "no rule matched → telegram fallback".
    """
    channels: set[str] = set()
    if isinstance(raw_channels, list):
        for channel_name in cast("list[object]", raw_channels):
            channels.add(str(channel_name))
    return channels or {"log_only"}


def _parse_rule_severity(severity_raw: object, pattern: str) -> PagerDutySeverity | None:
    """Parse a routing rule's ``severity_filter`` value, defaulting to ``warning``
    on an unrecognised string (with a log warning)."""
    if severity_raw is None:
        return None
    severity_str = str(severity_raw).lower()
    if severity_str in _VALID_SEVERITIES:
        return cast("PagerDutySeverity", severity_str)
    logger.warning(
        "Unknown severity %r in routing rule for %s, defaulting to 'warning'",
        severity_raw,
        pattern,
    )
    return "warning"


def _match_routing_rules(
    event_name: str,
    rules: list[dict[str, object]],
) -> tuple[set[str], PagerDutySeverity | None]:
    """Match event_name against routing rules and return channels + severity.

    Rules are evaluated in order; the FIRST matching rule wins. When no rule
    matches, fall back to telegram only.

    Returns:
        Tuple of (channel_set, pagerduty_severity_or_none).
    """
    for rule in rules:
        pattern = str(rule.get("event_pattern", ""))  # noqa: qg-empty-fallback
        if fnmatch(event_name, pattern):
            channels = _parse_rule_channels(rule.get("channels", []))  # noqa: qg-empty-fallback
            return channels, _parse_rule_severity(rule.get("severity_filter"), pattern)

    # No rule matched — fall back to Slack (#uts-live-alerts) so nothing fires silently.
    return {"slack"}, None


def _is_runtime_alert(event_name: str) -> bool:
    """Return True when event_name matches a specific LIVE_ALERT_RULES entry (runtime ops alert).

    Used by ``_deliver_to_uts_live_alerts_slack`` to gate #uts-live-alerts delivery:
    runtime/ops alerts are delivered; CI/QG/internal events are NOT (they have their
    own Slack channel via notify-slack.yml).

    The catch-all "*" rule (T4 INFO) is excluded — it exists to ensure nothing fires silently,
    not to mark all events as ops alerts. Only named patterns qualify for #uts-live-alerts.
    """
    return any(rule.event_pattern != "*" and fnmatch(event_name, rule.event_pattern) for rule in LIVE_ALERT_RULES)


def _deliver_to_uts_live_alerts_slack(
    event_name: str,
    summary: str,
    details: dict[str, object],
    config: AlertingSystemConfig,
) -> bool:
    """Deliver a live-ops runtime alert to the #uts-live-alerts Slack channel.

    Slack is the PRIMARY transport for generic/incident alerts (operator decision
    2026-06-23 — Telegram is RETIRED). Only LIVE_ALERT_RULES-matched runtime/ops
    alerts are delivered here; CI/QG/internal events are NOT (they have their own
    Slack channel via notify-slack.yml) and are intentionally a no-op.

    Returns True on success OR an intentional no-op (non-runtime event / no webhook
    configured in mock/CI); returns False ONLY on a real send failure, so the
    caller's delivery-failure accounting is accurate.

    SM-hot-reloaded webhook (alerting-uts-live-alerts-slack-webhook) takes
    precedence over the env-var value (UTS_LIVE_ALERTS_SLACK_WEBHOOK) when set.
    """
    if not _is_runtime_alert(event_name):
        return True  # CI/QG/internal — not a #uts-live-alerts incident (no-op, not a failure)
    sm_creds = get_paging_credentials()
    webhook = sm_creds.get("uts_live_alerts_slack_webhook") or config.uts_live_alerts_slack_webhook
    if not webhook:
        logger.warning("UTS-live-alerts Slack webhook not configured — cannot deliver %s", event_name)
        return True  # unconfigured (mock/CI) — best-effort no-op, not counted as a failure
    try:
        send_uts_live_alert(webhook, event_name, summary, details)
        return True
    except Exception as exc:
        logger.error("UTS-live-alerts Slack delivery failed for %s: %s", event_name, exc)
        return False


def _is_live_umbrella(details: dict[str, object]) -> bool:
    """Return True when this DP_*/DEPLOYMENT_* alert belongs to a LIVE-umbrella target.

    The umbrella (``LIVE`` / ``BATCH`` / ``PAPER`` / ``EXPERIMENT``) is stamped on the
    event payload by the emitter (deployment-service heartbeat + exit-code fleet
    monitor) via ``classify_deployment_target`` / ``umbrella_for_vm_name``. The
    routing split is umbrella-driven (operator 2026-06-23): **LIVE compute →
    ``#uts-live-alerts``**, **BATCH/PAPER/EXPERIMENT (or no umbrella) →
    ``#data-pipeline-alerts``**.

    Matching is case-insensitive on the LEADING ``live`` token so it accepts both the
    canonical StrEnum value ``LIVE`` and a legacy/derived ``live-<asset_group>`` token
    (e.g. ``live-defi``) without coupling to the exact emitter spelling. A missing /
    blank umbrella is BATCH-by-default (the data-pipeline channel) — the fail-safe
    direction (a batch alert in the live channel is louder noise than the reverse, and
    the live channel is the operator's smaller, higher-signal surface).
    """
    umbrella = details.get("umbrella")
    return isinstance(umbrella, str) and umbrella.strip().lower().startswith("live")


def _mirror_to_uts_live_alerts_slack_dp(
    event_name: str,
    summary: str,
    details: dict[str, object],
    config: AlertingSystemConfig,
) -> None:
    """Mirror a LIVE-umbrella data-pipeline / deployment alert to ``#uts-live-alerts``.

    The LIVE-umbrella counterpart of ``_mirror_to_data_pipeline_slack`` — a LIVE
    compute unit's failure/crash/hang/warning lands in the live-ops channel, never the
    batch one (operator 2026-06-23 routing contract). Best-effort, never raises (a
    Slack failure must not break the CRITICAL incident path, which fires separately).
    No-op when no webhook is configured (mock / CI / not-yet-provisioned).

    SM-hot-reloaded webhook (``alerting-uts-live-alerts-slack-webhook``) takes
    precedence over the env-var value (``UTS_LIVE_ALERTS_SLACK_WEBHOOK``) when set.
    """
    sm_creds = get_paging_credentials()
    webhook = sm_creds.get("uts_live_alerts_slack_webhook") or config.uts_live_alerts_slack_webhook
    if not webhook:
        return
    try:
        send_uts_live_alert(webhook, event_name, summary, details)
    except Exception as exc:
        # Mirror is strictly best-effort — never break the CRITICAL incident path.
        logger.warning("uts-live-alerts Slack mirror raised for %s: %s", event_name, exc)


def _mirror_to_data_pipeline_slack(
    event_name: str,
    summary: str,
    details: dict[str, object],
    config: AlertingSystemConfig,
) -> None:
    """Mirror a data-pipeline alert to the #data-pipeline-alerts Slack channel.

    Called for every DP_* / CONSOLIDATOR_DOWN event (matched by
    ``data_pipeline_rule_for``) — the start-verbose channel of
    ``data_pipeline_hardening_self_monitoring_2026_06_22.md``. CRITICAL events
    ALSO page via the incident path (see ``_route_data_pipeline_event``); this
    is the channel mirror, not the only sink.

    Best-effort: a Slack failure never affects the CRITICAL incident path.
    No-op when no webhook is configured (mock / CI / not-yet-provisioned).

    SM-hot-reloaded webhook (DATA_PIPELINE_ALERTS_SLACK_WEBHOOK) takes
    precedence over the env-var value (data_pipeline_slack_webhook) when set.

    The ``deployment_ui_base_url`` + ``deployment_scripts_log_bucket`` (SM-first,
    env-fallback) are threaded into the notifier so the alert carries
    click-through deep-links (VM logs / deployment detail / data status / GCS
    run.log). Both empty → links omitted (no broken link).
    """
    sm_creds = get_paging_credentials()
    webhook = sm_creds.get("data_pipeline_slack_webhook") or config.data_pipeline_slack_webhook
    if not webhook:
        return
    deployment_ui_base_url = sm_creds.get("deployment_ui_base_url") or config.deployment_ui_base_url
    log_bucket = sm_creds.get("deployment_scripts_log_bucket") or config.deployment_scripts_log_bucket
    try:
        send_data_pipeline_alert(
            webhook,
            event_name,
            summary,
            details,
            deployment_ui_base_url=deployment_ui_base_url,
            log_bucket=log_bucket,
        )
    except Exception as exc:
        # Mirror is strictly best-effort — never break the CRITICAL incident path.
        logger.warning("data-pipeline-alerts Slack mirror raised for %s: %s", event_name, exc)


def _route_data_pipeline_event(
    event_name: str,
    summary: str,
    details: dict[str, object],
    severity: AlertSeverity,
    config: AlertingSystemConfig,
) -> None:
    """Route a matched data-pipeline / deployment alert: channel mirror + CRITICAL page.

    **Umbrella-driven channel split (operator 2026-06-23)** — the channel mirror
    follows the deployment umbrella stamped on the payload:

      * ``umbrella == LIVE``  → mirror to ``#uts-live-alerts`` (live-ops surface);
      * everything else (``BATCH`` / ``PAPER`` / ``EXPERIMENT`` / no umbrella)
        → mirror to ``#data-pipeline-alerts`` (the batch surface, unchanged default).

    So EVERY VM / Cloud-Run-job issue (failure / crash exit-137 OOM / hang / WARN /
    ERROR) propagates to the RIGHT channel: BATCH compute → #data-pipeline-alerts,
    LIVE compute → #uts-live-alerts. A DP_* alert with no umbrella stays on the batch
    channel exactly as before (no behaviour change for the existing DP_* family).

    For ``severity == CRITICAL``, ALSO routes through the existing incident plumbing
    (``route_event_with_explicit_channels`` → PagerDuty + Telegram) — the SAME path
    consolidator-rules CRITICAL events use; dedup / ack / persistence are reused, never
    forked. INFO/WARN stay channel-only (the WARN dedup was already applied by
    ``route_event`` before this is called). The CRITICAL page fires for BOTH umbrellas
    (a live OR batch CRITICAL pages) — only the Slack CHANNEL differs by umbrella.

    A STATIC BACKLOG DP_RUN_MOSTLY_EMPTY cell is downgraded to WARN here first
    (see ``dp_run_mostly_empty_static_backlog.effective_severity``), taking the
    INFO/WARN channel-only path instead of paging.
    """
    severity = _effective_dp_severity(event_name, details, severity)
    details_with_sev: dict[str, object] = {**details, "severity": severity.value}
    if _is_live_umbrella(details_with_sev):
        _mirror_to_uts_live_alerts_slack_dp(event_name, summary, details_with_sev, config)
    else:
        _mirror_to_data_pipeline_slack(event_name, summary, details_with_sev, config)

    if severity is AlertSeverity.CRITICAL:
        route_event_with_explicit_channels(
            event_name,
            details_with_sev,
            channels={"pagerduty", "telegram"},
            pd_severity="critical",
        )


def _persisted_severity(pd_severity: PagerDutySeverity | None, details: dict[str, object]) -> str | None:
    """Resolve the severity to persist: the PagerDuty-normalised tier when known,
    else whatever the emit site stamped on ``details['severity']``."""
    if pd_severity is not None:
        return pd_severity
    raw = details.get("severity")
    return str(raw) if raw is not None else None


def _extract_deployment_target(details: dict[str, object]) -> str | None:
    """Extract the VM/deployment target an alert concerns, if any.

    Mirrors the ``vm_name``/``deployment_id`` convention deployment-api's reader
    already parses from ``details`` (``_repo_ci_alerts.py``) so downstream
    ingestion needs no per-source translation.
    """
    target = details.get("vm_name") or details.get("vm") or details.get("deployment_id")
    return str(target) if target else None


def _build_delivery_record(
    alert_id: str,
    channel: str,
    status: str,
    response_detail: str,
    event_name: str,
    *,
    severity: str | None = None,
    message: str | None = None,
    service: str | None = None,
    deployment_target: str | None = None,
) -> dict[str, object]:
    """Build an AlertDeliveryRecord dict.

    Beyond delivery status (``channel``/``status``/``response_detail``), also carries
    the normalised-schema fields (decision 2,
    ``deployment_alerts_ingestion_completeness_2026_07_20.md``) so a delivery record
    isn't detail-poorer than the decision record: ``alert_class`` (the event_name),
    plus ``severity``/``message``/``service``/``deployment_target`` when the emit site
    provides them.
    """
    return {
        "alert_id": alert_id,
        "channel": channel,
        "status": status,
        "response_detail": response_detail,
        "event_name": event_name,
        "alert_class": event_name,
        "severity": severity,
        "message": message,
        "service": service,
        "deployment_target": deployment_target,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _route_synthetic_log_only(
    *,
    event_name: str,
    details: dict[str, object],
    alert_id: str,
    source: str,
) -> None:
    """Synthetic-event short-circuit: log + persist record, NO channel dispatch.

    Phase 3.F (``simulation_scenarios_topology_price_shocks_2026_05_09.md``):
    scenario-fire alerts carry ``synthetic=True`` in their details payload.
    The router emits an ``ALERT_SUPPRESSED_SYNTHETIC`` audit event + persists
    a log-only AlertDeliveryRecord (so operator-visible dashboards still see
    the synthetic alert) but skips PagerDuty + Telegram dispatch.

    Per CLAUDE.md "Live = batch": real-fire alerts still page normally — the
    ONLY distinction from prod alerts is the ``synthetic`` flag on the
    payload.

    Does NOT call ``_get_cloud_config`` / ``_persist_config_snapshot`` — the
    synthetic short-circuit must work in scenario runs that may not have GCP /
    AWS credentials available (CI / local mock runs).
    """
    log_event(
        "ALERT_SUPPRESSED_SYNTHETIC",
        details={
            "event_name": event_name,
            "alert_id": alert_id,
            "source": source,
            "scenario_id": str(details.get("scenario_id", "")),  # noqa: qg-empty-fallback
            "reason": "synthetic=True in alert payload — paging suppressed per Phase 3.F",
        },
    )
    if _batch_mode:
        # Batch mode: record the synthetic dispatch in the per-batch audit log
        # using the LOG_ONLY channel set. Mirrors _record_batch_audit's shape
        # without paging-channel resolution.
        _record_batch_audit(alert_id, event_name, {"log_only"}, None, source, details)
        return
    record = _build_delivery_record(
        alert_id=alert_id,
        channel="log_only",
        status="suppressed_synthetic",
        response_detail="paging suppressed — synthetic=True",
        event_name=event_name,
        severity=_persisted_severity(None, details),
        message=str(details.get("message", event_name)),
        service=source,
        deployment_target=_extract_deployment_target(details),
    )
    _persist_delivery_record(record)


def _persist_delivery_record(record: dict[str, object]) -> None:
    """Write a delivery record to GCS history (best-effort)."""
    try:
        store = _get_storage_store()
        store.write_alert_history(record)
    except Exception as exc:
        classify_and_emit_error(
            exc,
            service_name="alerting-service",
            operation="persist_delivery_record",
        )


def _persist_config_snapshot(config: AlertingSystemConfig) -> None:
    """Write current routing config to GCS configs/ (best-effort)."""
    try:
        store = _get_storage_store()
        snapshot: dict[str, object] = {"routing_rules": config.routing_rules}
        store.write_config_snapshot(snapshot, name="routing_rules")
    except Exception as exc:
        classify_and_emit_error(
            exc,
            service_name="alerting-service",
            operation="persist_config_snapshot",
        )


def _record_batch_audit(
    alert_id: str,
    event_name: str,
    channels: set[str],
    pd_severity: PagerDutySeverity | None,
    source: str,
    details: dict[str, object],
) -> None:
    """Record what would have been delivered in batch mode (no actual delivery)."""
    global _batch_matched
    _batch_matched += 1
    for ch in channels:
        _batch_would_deliver[ch] = _batch_would_deliver.get(ch, 0) + 1
    _persist_delivery_record(
        {
            "alert_id": alert_id,
            "event_name": event_name,
            "alert_class": event_name,
            "channels": sorted(channels),
            "severity": _persisted_severity(pd_severity, details),
            "message": str(details.get("message", event_name)),
            "service": source,
            "deployment_target": _extract_deployment_target(details),
            "status": "batch_audit",
            "response_detail": "delivery_suppressed_batch_mode",
            "source": source,
            "original_timestamp": str(details.get("timestamp", "")),  # noqa: qg-empty-fallback
            "timestamp": datetime.now(UTC).isoformat(),
            "_batch_replay": True,
        }
    )


# Kill-switch rule matching + bus publish live in ``kill_switch_rules.py``
# (split 2026-06-12, codex ratchet plan Phase 1.5). Re-bound under the original
# private names: tests patch ``router._publish_kill_switch_event`` and the
# route_event call below resolves it from THIS module's globals at call time,
# so the patch surface is unchanged.
from alerting_service.notifiers.kill_switch_rules import (
    publish_kill_switch_event as _publish_kill_switch_event,
)


def route_event(event_name: str, details: dict[str, object]) -> None:
    """Route an event to the correct notifier(s) based on config-driven rules.

    Deduplication: events with the same name and details hash are
    suppressed for 60 seconds.

    Routing rules are read from ``AlertingSystemConfig.routing_rules``.
    The first matching rule (by fnmatch glob) determines which channels
    receive the event.

    Each delivery attempt produces an ``AlertDeliveryRecord`` persisted
    to GCS history.

    Args:
        event_name: Canonical event name (e.g. "KILL_SWITCH_ACTIVATED").
        details: Arbitrary context dictionary forwarded to the notifier payload.
    """
    global _batch_deduplicated, _batch_matched

    if _deduplicator.is_duplicate(event_name, details, ttl_override=_dedup_window_for(event_name, details)):
        logger.debug("Duplicate alert suppressed: %s", event_name)
        if _batch_mode:
            _batch_deduplicated += 1
        return

    # Tick-staleness + connectivity-gap coalesce window — merge concurrent
    # TICK_STALENESS + CONNECTIVITY_GAP_DETECTED fires on the same
    # (venue, instrument) within 30s into ONE operator-visible alert.
    # See ``_check_coalesce_window`` docstring above; non-staleness /
    # non-connectivity events pass through unchanged.
    if _check_coalesce_window(event_name, details, time.monotonic()):
        log_event(
            "ALERT_COALESCED",
            details={
                "event_name": event_name,
                "venue": details.get("venue"),
                "instrument": details.get("instrument"),
                "window_seconds": _COALESCE_WINDOW_SECONDS,
            },
        )
        return

    log_event("ALERT_ROUTED", details={"event_name": event_name})

    alert_id = uuid.uuid4().hex[:16]
    source = str(details.get("source", "alerting-service"))

    # Phase 3.F synthetic-paging-suppression: log + persist + short-circuit
    # BEFORE _get_cloud_config + channel resolution + dispatch when details
    # carries synthetic=True. Avoids touching cloud config during synthetic
    # scenario runs (which may run without GCP/AWS credentials in CI).
    if _is_synthetic(details):
        _route_synthetic_log_only(
            event_name=event_name,
            details=details,
            alert_id=alert_id,
            source=source,
        )
        return

    config = _get_cloud_config()
    summary = f"[{event_name}] {details.get('message', event_name)}"

    # Data-pipeline alert (DP_* family + CONSOLIDATOR_DOWN) — mirror to
    # #data-pipeline-alerts; CRITICAL ones ALSO page via the incident path.
    # Short-circuits the generic routing rules (a DP_* event must NOT also
    # mirror to #uts-live-alerts / fall through to the telegram catch-all).
    # CI/QG notify-slack.yml events are NOT in DATA_PIPELINE_ALERT_RULES, so
    # they are untouched by this branch.
    #
    # Lazy import (deferred to call time, like coalesce / kill_switch_rules
    # below): the rules package imports router at module load (consolidator_rules
    # needs route_event_with_explicit_channels), so a top-level
    # `from ..rules.data_pipeline_rules import …` would form a partial-init cycle.
    from alerting_service.rules.data_pipeline_rules import data_pipeline_rule_for

    dp_rule = data_pipeline_rule_for(event_name)
    if dp_rule is not None:
        if _batch_mode:
            # Apply the same STATIC BACKLOG downgrade as the live path (see _effective_dp_severity)
            # so batch-replay audit stats don't count a suppressed page as paged.
            dp_severity = _effective_dp_severity(event_name, details, dp_rule.severity)
            dp_channels = {ch.value for ch in dp_rule.channels} or {AlertChannel.LOG_ONLY.value}
            dp_pd = "critical" if dp_severity is AlertSeverity.CRITICAL else None
            _record_batch_audit(alert_id, event_name, dp_channels, dp_pd, source, details)
            return
        if dp_rule.mirror_live:  # False (2026-08-07): still tracked, skips the live post
            _route_data_pipeline_event(event_name, summary, details, dp_rule.severity, config)
        sent: dict[str, object] = {"event_name": event_name, "alert_id": alert_id, "mirrored_live": dp_rule.mirror_live}
        log_event("ALERT_SENT", details=sent)
        _persist_config_snapshot(config)
        return

    # Deployment lifecycle alert (DEPLOYMENT_STARTED/COMPLETED/FAILED) — Slack
    # parity at /repos grade: the #data-pipeline-alerts mirror with the umbrella
    # + cloud + a /deployments/{name} deep-link. FAILED is CRITICAL → ALSO pages
    # via the shared incident path. Reuses _route_data_pipeline_event (same
    # dedup / persistence / deep-link enrichment), never forked. Lazy import for
    # the same partial-init-cycle reason as data_pipeline_rules above.
    from alerting_service.rules.deployment_rules import deployment_rule_for

    deploy_rule = deployment_rule_for(event_name)
    if deploy_rule is not None:
        if _batch_mode:
            dep_pd = "critical" if deploy_rule.severity is AlertSeverity.CRITICAL else None
            dep_channels = (
                {AlertChannel.SLACK.value, AlertChannel.TELEGRAM.value, AlertChannel.PAGERDUTY.value}
                if deploy_rule.severity is AlertSeverity.CRITICAL
                else {AlertChannel.SLACK.value}
            )
            _record_batch_audit(alert_id, event_name, dep_channels, dep_pd, source, details)
            return
        _route_data_pipeline_event(event_name, summary, details, deploy_rule.severity, config)
        log_event("ALERT_SENT", details={"event_name": event_name, "alert_id": alert_id})
        _persist_config_snapshot(config)
        return

    # Determine channels from config-driven routing rules
    channels, pd_severity = _match_routing_rules(event_name, config.routing_rules)

    if _batch_mode:
        _record_batch_audit(alert_id, event_name, channels, pd_severity, source, details)
        return

    failed = _deliver_to_channels(
        alert_id,
        event_name,
        summary,
        source,
        channels,
        pd_severity,
        config,
        details,
    )
    status_event = "ALERT_FAILED" if failed else "ALERT_SENT"
    log_event(status_event, details={"event_name": event_name, "alert_id": alert_id})

    # Kill-switch publisher hook — runs AFTER channel dispatch so paging fires
    # even if the bus publish errors. Side-effect-free vs channel dispatch.
    _publish_kill_switch_event(event_name, details, alert_id)

    _persist_config_snapshot(config)


def route_event_with_explicit_channels(
    event_name: str,
    details: dict[str, object],
    *,
    channels: set[str],
    pd_severity: PagerDutySeverity | None,
) -> None:
    """Route an event to an explicitly-supplied channel set, bypassing config rules.

    Used by consumers that compute the severity tier themselves rather than
    relying on a matching ``LIVE_ALERT_RULES`` ``event_pattern`` — e.g. the
    disaster-recovery kill-switch arm/disarm + circuit-breaker fire handlers,
    where the severity is a function of ``KillSwitchId`` scope / ``BreakerAction``
    rather than of a static AlertCode.

    Shares the dedup / persistence / log-event machinery with :func:`route_event`;
    the ONLY difference is that channel resolution is supplied by the caller.
    ``channels == {"log_only"}`` (or any set excluding pagerduty/telegram) results
    in no delivery — only the ``ALERT_ROUTED`` / ``ALERT_SENT`` audit trail.

    Dedup now consults ``_dedup_window_for`` too (2026-08-06 fix) — this path
    used to sit on the deduplicator's bare 60s default, so CONSOLIDATOR_DOWN
    (dispatched here, bypassing ``route_event``'s cooldown check) re-fired
    ~every 60s instead of respecting its opted-in hourly cooldown.
    """
    global _batch_deduplicated

    if _deduplicator.is_duplicate(event_name, details, ttl_override=_dedup_window_for(event_name, details)):
        logger.debug("Duplicate alert suppressed (explicit channels): %s", event_name)
        if _batch_mode:
            _batch_deduplicated += 1
        return

    log_event("ALERT_ROUTED", details={"event_name": event_name})

    alert_id = uuid.uuid4().hex[:16]
    source = str(details.get("source", "alerting-service"))

    # Phase 3.F synthetic-paging-suppression: log + persist + short-circuit
    # BEFORE _get_cloud_config + channel dispatch when details carries
    # synthetic=True.
    if _is_synthetic(details):
        _route_synthetic_log_only(
            event_name=event_name,
            details=details,
            alert_id=alert_id,
            source=source,
        )
        return

    config = _get_cloud_config()
    summary = f"[{event_name}] {details.get('message', event_name)}"

    if _batch_mode:
        _record_batch_audit(alert_id, event_name, channels, pd_severity, source, details)
        return

    failed = _deliver_to_channels(
        alert_id,
        event_name,
        summary,
        source,
        channels,
        pd_severity,
        config,
        details,
    )
    status_event = "ALERT_FAILED" if failed else "ALERT_SENT"
    log_event(status_event, details={"event_name": event_name, "alert_id": alert_id})
    _persist_config_snapshot(config)


def _deliver_to_channels(
    alert_id: str,
    event_name: str,
    summary: str,
    source: str,
    channels: set[str],
    pd_severity: PagerDutySeverity | None,
    config: AlertingSystemConfig,
    details: dict[str, object],
) -> bool:
    """Deliver to all matched channels. Returns True if any delivery failed."""
    any_failed = False

    pd_suppressed = config.pagerduty_disabled or config.quietness_baseline_mode
    if "pagerduty" in channels and not pd_suppressed:
        severity: PagerDutySeverity = pd_severity or "critical"
        ok = pd_send_event(summary=summary, severity=severity, source=source, details=details)
        if not ok:
            logger.error("PagerDuty delivery failed for event %s", event_name)
            any_failed = True
            # CRITICAL last-resort fallback (2026-08-06): PD unavailable/failing ->
            # email (log_event observability lives in notifiers/email.py).
            if severity == "critical" and not send_critical_fallback(summary, source, details, config):
                logger.error("Email fallback ALSO failed for CRITICAL event %s — undelivered", event_name)
        _persist_delivery_record(
            _build_delivery_record(
                alert_id,
                "pagerduty",
                "sent" if ok else "failed",
                "accepted" if ok else "delivery_failed",
                event_name,
                severity=severity,
                message=summary,
                service=source,
                deployment_target=_extract_deployment_target(details),
            )
        )
    elif "pagerduty" in channels and pd_suppressed:
        logger.info(
            "PagerDuty suppressed (quietness_baseline_mode/pagerduty_disabled) for event %s",
            event_name,
        )

    # Slack is the PRIMARY transport for generic/incident alerts (operator decision
    # 2026-06-23; Telegram RETIRED). "telegram" channel names from existing routing
    # rules + the no-match catch-all both map here so nothing fires silently.
    if "slack" in channels or "telegram" in channels or not channels:
        # PagerDuty SHADOW annotation (calibration): state how this alert WOULD escalate
        # to PagerDuty so the operator can tune PD routing/severity before enabling it.
        if "pagerduty" in channels:
            _pd_sev = pd_severity or "critical"
            _pd_state = "currently DISABLED — calibration" if pd_suppressed else "ENABLED"
            pd_shadow = f"WOULD PAGE → severity=`{_pd_sev}` ({_pd_state})"
        else:
            pd_shadow = "no PagerDuty escalation (Slack-only event)"
        ok = _deliver_to_uts_live_alerts_slack(event_name, summary, {**details, "_pagerduty_shadow": pd_shadow}, config)
        if not ok:
            any_failed = True
        _persist_delivery_record(
            _build_delivery_record(
                alert_id,
                "slack",
                "sent" if ok else "failed",
                "accepted" if ok else "delivery_failed",
                event_name,
                severity=_persisted_severity(pd_severity, details),
                message=summary,
                service=source,
                deployment_target=_extract_deployment_target(details),
            )
        )

    return any_failed


# ──────────────────────────────────────────────────────────────────────────
# IncidentEnvelope-aware routing (typed entry point — Phase 1 P0.1)
# ──────────────────────────────────────────────────────────────────────────
#
# Typed entry point for the Incident Gateway state machine. Existing dict-
# shape callers (error_event_handler, dr_event_handler, etc.) continue to use
# route_event() unchanged — this function is the NEW canonical path for
# gateway-originated alerts.
#
# Design: route_incident() normalises IncidentEnvelope to the standard
# (event_name, details) shape and delegates to the existing routing machinery
# (dedup, coalesce, config rules, kill-switch hook). This guarantees that
# typed incidents share the same audit trail + dedup window + channel
# selection as legacy dict-shape events.


def route_incident(envelope: IncidentEnvelope) -> None:
    """Route an IncidentEnvelope through the notifier chain.

    Pre-evaluates ImmediateSev0Override predicates (P0.14) and the
    AUTO_ACTION_SUCCEEDED recovery-verification gate (P0.13) before
    dispatching to standard channel routing machinery.

    Uses ``envelope.problem_type`` as the routing key (fnmatch against
    ``AlertingSystemConfig.routing_rules``). KILL_SWITCH_* problem_types
    continue to trigger the kill-switch publisher hook.
    """
    # P0.14 — ImmediateSev0Override pre-evaluation: any override forces SEV0
    # routing (Twilio voice + physical pager) regardless of severity_hint.
    sev0_overrides = _extract_sev0_overrides(envelope)
    if sev0_overrides:
        _dispatch_sev0_fallbacks(envelope, sev0_overrides)

    # P0.13 — AUTO_ACTION_SUCCEEDED gate: must go through recovery_verifier
    # before routing. Function handles state transitions + recursive routing.
    if envelope.state is IncidentState.AUTO_ACTION_SUCCEEDED:
        _handle_auto_action_recovery(envelope)
        return

    _route_envelope_to_channels(envelope)


def _route_envelope_to_channels(envelope: IncidentEnvelope) -> None:
    """Build details dict from envelope and dispatch to route_event()."""
    details: dict[str, object] = {
        "message": envelope.problem_summary,
        "incident_key": envelope.incident_key,
        "event_id": envelope.event_id,
        "service": envelope.service,
        "component": envelope.component,
        "severity": envelope.severity_hint.value,
        "domain": envelope.domain,
        "environment": envelope.environment,
        "state": envelope.state.value,
        "risk_state": envelope.risk_state,
        "capital_at_risk": envelope.capital_at_risk,
        "auto_action_allowed": envelope.auto_action_allowed,
        "source": envelope.service,
    }
    if envelope.strategy_id is not None:
        details["strategy_id"] = envelope.strategy_id
    if envelope.venue is not None:
        details["venue"] = envelope.venue
    if envelope.account_id is not None:
        details["account_id"] = envelope.account_id
    if envelope.instrument_id is not None:
        details["instrument"] = envelope.instrument_id
    if envelope.runbook_id is not None:
        details["runbook_id"] = envelope.runbook_id
    route_event(envelope.problem_type, details)


# ── P0.14 — ImmediateSev0Override helpers ─────────────────────────────────


def _extract_sev0_overrides(envelope: IncidentEnvelope) -> tuple[str, ...]:
    """Return (problem_type,) if it is a closed-set ImmediateSev0Override; else ()."""
    try:
        ImmediateSev0Override(envelope.problem_type)
        return (envelope.problem_type,)
    except ValueError:
        return ()


def _dispatch_sev0_fallbacks(envelope: IncidentEnvelope, overrides: tuple[str, ...]) -> None:
    """Fire Twilio voice + physical pager for a SEV0 override. Never raises."""
    result = route_incident_envelope_to_fallbacks(envelope, immediate_sev0_overrides=overrides)
    log_event(
        "SEV0_OVERRIDE_DISPATCHED",
        details={
            "incident_key": envelope.incident_key,
            "override": overrides[0] if overrides else "",
            "twilio_voice": result.get("twilio_voice"),
            "physical_pager": result.get("physical_pager"),
        },
    )


# ── P0.13 — AUTO_ACTION_SUCCEEDED recovery-verification gate ──────────────

_recovery_verifier_singleton: object | None = None
_state_machine_singleton: object | None = None


def _get_recovery_verifier() -> object:
    """Return lazy-initialised module-level RecoveryVerifier."""
    global _recovery_verifier_singleton
    if _recovery_verifier_singleton is None:
        _recovery_verifier_singleton = RecoveryVerifier()
    return _recovery_verifier_singleton


def _get_incident_state_machine() -> object:
    """Return lazy-initialised module-level IncidentStateMachine."""
    global _state_machine_singleton
    if _state_machine_singleton is None:
        _state_machine_singleton = IncidentStateMachine()
    return _state_machine_singleton


_SEV_LADDER: tuple[AlertSeverity, ...] = (
    AlertSeverity.INFO,
    AlertSeverity.WARN,
    AlertSeverity.HIGH,
    AlertSeverity.CRITICAL,
)


def _escalate_severity(severity: AlertSeverity) -> AlertSeverity:
    """Bump severity one tier up; CRITICAL stays CRITICAL."""
    idx = _SEV_LADDER.index(severity) if severity in _SEV_LADDER else 0
    return _SEV_LADDER[min(idx + 1, len(_SEV_LADDER) - 1)]


def _handle_auto_action_recovery(
    envelope: IncidentEnvelope,
    *,
    _sm: object | None = None,
    _verifier: object | None = None,
) -> None:
    """Drive state machine through recovery-verification gate.

    AUTO_ACTION_SUCCEEDED → RECOVERY_VERIFICATION_STARTED
        → RECOVERY_CONFIRMED (all 5 checks pass) → route; OR
        → RECOVERY_UNCERTAIN (any check fails) → escalate severity + route

    ``_sm`` / ``_verifier`` are test-injection points; production code always
    passes None (uses module-level singletons).
    """
    sm = cast(
        "IncidentStateMachine",
        _sm if _sm is not None else _get_incident_state_machine(),
    )
    verifier = cast(
        "RecoveryVerifier",
        _verifier if _verifier is not None else _get_recovery_verifier(),
    )

    verif_result = sm.transition(envelope, IncidentState.RECOVERY_VERIFICATION_STARTED)
    if not verif_result.succeeded:
        logger.warning(
            "AUTO_ACTION_SUCCEEDED→RECOVERY_VERIFICATION_STARTED failed: %s incident=%s",
            verif_result.failure_reason,
            envelope.incident_key,
        )
        return

    verif_env = verif_result.envelope
    scope: dict[str, str] = {}
    if verif_env.strategy_id:
        scope["strategy_id"] = verif_env.strategy_id
    if verif_env.venue:
        scope["venue"] = verif_env.venue

    rv = verifier.verify(verif_env.incident_key, scope)
    if rv.all_passed:
        confirmed = sm.transition(verif_env, IncidentState.RECOVERY_CONFIRMED)
        if confirmed.succeeded:
            _route_envelope_to_channels(confirmed.envelope)
    else:
        reasons = ", ".join(rv.failure_reasons) or "recovery checks failed"
        uncertain = sm.transition(
            verif_env,
            IncidentState.RECOVERY_UNCERTAIN,
            severity_hint=_escalate_severity(verif_env.severity_hint),
        )
        if uncertain.succeeded:
            logger.warning("Recovery UNCERTAIN for incident=%s: %s", envelope.incident_key, reasons)
            _route_envelope_to_channels(uncertain.envelope)


# ── P0.12 — Backward-compat shim ──────────────────────────────────────────


def route_legacy_alert(
    payload: "dict[str, object] | object",
    *,
    fallback_service: str = "unknown",
) -> None:
    """Wrap a legacy raw-alert dict (or Pydantic model) into an IncidentEnvelope
    and route it via ``route_incident()``.

    Emitters that pre-date the Tier-1 UAC schemas continue to call
    ``route_event()`` directly; this shim lets callers migrate to the typed
    path without a big-bang refactor on the emitter side.
    """
    envelope = wrap_legacy_alert(payload, fallback_service=fallback_service)
    route_incident(envelope)
