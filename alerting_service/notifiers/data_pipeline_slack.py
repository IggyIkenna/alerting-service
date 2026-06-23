"""Data-pipeline alerts → Slack mirror notifier.

Data-pipeline self-monitoring alerts (the ``DP_*`` family + ``CONSOLIDATOR_DOWN``,
matched by UAC ``DATA_PIPELINE_ALERT_RULES``) are mirrored to the
``#data-pipeline-alerts`` Slack channel so operators watching Slack see the
pipeline's honest-absence gate trips, VM exit-code failures, non-canonical
writes, rate-limit stalls, etc.

The webhook belongs to a dedicated ``data-pipeline-alerts`` Slack app/channel —
distinct from the ``#uts-live-alerts`` mirror (``uts_live_alerts_slack.py``).
The webhook is hot-reloaded from Secret Manager
(``DATA_PIPELINE_ALERTS_SLACK_WEBHOOK``) via ``config_reloaders.py``.

Delivery is best-effort and never raises: when no webhook is configured (mock /
CI / not-yet-provisioned), the notifier no-ops. This is the start-verbose
channel of `data_pipeline_hardening_self_monitoring_2026_06_22.md` Phase 0/2;
CRITICAL events ALSO route through the existing incident path (PagerDuty +
Telegram) — this notifier is the channel mirror, not the only sink.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
from unified_trading_library import log_event

logger = logging.getLogger(__name__)

# Pre-sleep delays for the 2nd and 3rd attempts (no sleep before the 1st).
# Mirrors notifiers/uts_live_alerts_slack.py retry semantics.
_BACKOFF_SECS: tuple[float, ...] = (0.5, 1.0)

# Slack section/code-block text hard limit is 3000 chars — the inline trace
# block is truncated to stay under it (with a marker so the operator knows it
# was clipped + where the full trace lives).
_SLACK_TEXT_LIMIT = 3000
_TRUNCATION_MARKER = "\n… [truncated — see VM logs / run.log for the full trace]"

# Detail keys whose value (when present) is pasted into the fenced-code trace
# block, in priority order — the most-informative payload first.
_TRACE_KEYS: tuple[str, ...] = (
    "evidence",
    "fetch_evidence",
    "error_message",
    "error",
    "stack_trace",
    "run_log_tail",
)

_SEVERITY_EMOJI: dict[str, str] = {
    "critical": ":rotating_light:",
    "error": ":x:",
    "high": ":x:",
    "warning": ":warning:",
    "warn": ":warning:",
    "info": ":information_source:",
}

# Per-event human-readable "what happened + what to do" lines. Keyed on the
# canonical DP_*/CONSOLIDATOR/DEPLOYMENT event name. The value is a (what, action)
# pair rendered as a "*What:* … / *Action:* …" section so a non-author operator
# can read + act without opening the code. Unknown events fall back to the
# generic line (still names the event + says where to look). Codified
# 2026-06-23 (operator: alerts must be human-readable + actionable).
_EVENT_EXPLAIN: dict[str, tuple[str, str]] = {
    "DP_VM_EXIT_NONZERO": (
        "A data-pipeline / backfill VM terminated with a NON-ZERO exit code "
        "(137 = OOM-killed; other = crash/error). Its captured rows did not "
        "complete cleanly.",
        "Open the run.log link below for the error/traceback. If the exit code is "
        "137 (OOM) the OOM auto-recover may relaunch it (≤2/day per prefix) — "
        "confirm a relaunch fired; otherwise relaunch with a larger machine type / "
        "smaller shard. A non-OOM crash needs a code/config fix before relaunch.",
    ),
    "DP_VM_GONE_NO_CAPTURE": (
        "A VM drained/self-deleted but its manifest captured count did NOT climb "
        "— a self-delete masked a zero-row run (silent failure).",
        "Open the run.log link for why it captured nothing (auth / 0-universe / "
        "source error), fix the root cause, and re-run the backfill for that "
        "asset_group / date range.",
    ),
    "DP_CRON_DID_NOT_FIRE": (
        "A scheduled audit / consolidator / digest cron missed its expected "
        "window (its durable heartbeat/output artifact is stale) — the cron "
        "stopped firing.",
        "Check the named cron's Cloud Scheduler / Cloud Run job execution history "
        "(use the deep-link), confirm it is enabled + not erroring, and trigger a "
        "manual run to catch up.",
    ),
    "DP_CATALOG_NOT_RUNNING": (
        "The instrument catalogue artifact for this asset_group has not been "
        "refreshed within its freshness budget — the enumerator/catalogue regen "
        "stopped, so downstream capture has a stale could-exist universe.",
        "Re-run the instruments-service catalogue/enumerator for the named "
        "asset_group; verify the scheduled catalogue refresh job is healthy.",
    ),
    "CONSOLIDATOR_DOWN": (
        "The manifest consolidator (Cloud Run Job) is not running / is stale — "
        "the consolidated availability index is going stale while per-VM shards "
        "accumulate.",
        "Check the manifest_consolidator Cloud Run Job + its scheduler; re-run the "
        "consolidation job to rebuild the index.",
    ),
    "DP_SOURCE_RATE_LIMITED": (
        "A source (e.g. API-Football / a subgraph) returned an HTTP-429 / rate-limit "
        "throttle, so the run captured a partial or empty result — this is a "
        "real-transient, NOT a silent failure.",
        "Back off and retry on a longer interval (do NOT relaunch immediately — a "
        "relaunch re-hits the same per-minute/day limit). Verify the source's quota / "
        "key-pool rotation; if it persists, raise the tier or stagger the schedule.",
    ),
}


def _build_explain_block(event_name: str, details: dict[str, object]) -> dict[str, object] | None:
    """Build a "*What:* … / *Action:* …" section explaining the alert in plain terms.

    Returns None for an unknown event with no generic context to add (so a clean
    event isn't padded). A known DP_* event always renders the curated
    what/action pair — the core of "human-readable + actionable".
    """
    explain = _EVENT_EXPLAIN.get(event_name)
    if explain is None:
        return None
    what, action = explain
    text = f"*What happened:* {what}\n*Recommended action:* {action}"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _emoji_for(event_name: str, severity: str | None) -> str:
    """Pick a Slack emoji from severity, falling back to event-name heuristics."""
    if severity is not None:
        emoji = _SEVERITY_EMOJI.get(severity.lower())
        if emoji is not None:
            return emoji
    upper = event_name.upper()
    if "UNPROVEN" in upper or "EXIT_NONZERO" in upper or "NONCANONICAL" in upper:
        return ":rotating_light:"
    if "FAILED" in upper or "ERROR" in upper or "DOWN" in upper:
        return ":x:"
    return ":warning:"


def _stringify_trace(value: object) -> str:
    """Render a trace value as text: dicts/lists → pretty JSON, else str().

    ``default=str`` makes ``json.dumps`` total for arbitrary nested payloads, so
    the except path is a belt-and-braces guard that never serialises the raw
    value (which carries unknown element types).
    """
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, indent=2, sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            return f"<unserialisable trace: {exc}>"
    return str(value)


def _build_trace_block(details: dict[str, object]) -> dict[str, object] | None:
    """Build a fenced-code trace block from the most-informative ``details`` key.

    Pastes the first present key in ``_TRACE_KEYS`` (the FetchEvidence dict, the
    exit_code+run_log_tail, or the error_message) into a Slack code block,
    truncated to ``_SLACK_TEXT_LIMIT`` chars. Returns None when no trace payload
    is present (so a clean event has no empty trace block).
    """
    for key in _TRACE_KEYS:
        value = details.get(key)
        if value in (None, "", {}, []):
            continue
        trace = _stringify_trace(value)
        if not trace.strip():
            continue
        if len(trace) > _SLACK_TEXT_LIMIT:
            keep = _SLACK_TEXT_LIMIT - len(_TRUNCATION_MARKER) - len("```\n\n```")
            trace = trace[:keep] + _TRUNCATION_MARKER
        text = f"*Trace ({key}):*\n```\n{trace}\n```"
        return {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    return None


def _build_action_block(
    details: dict[str, object],
    deployment_ui_base_url: str,
    log_bucket: str,
) -> dict[str, object] | None:
    """Build a context block of click-through deep-links from ``details``.

    Links are emitted only when their inputs are present (no broken links):
      - ``vm_name`` → "VM logs" ({base}/ops/vms/{vm}) + "Deployment"
        ({base}/deployments/{vm}) + GCS run.log console link (when ``log_bucket``);
      - ``asset_group`` → "Data status"
        ({base}/service/{service}/data-status?asset_group={ag}).

    ``deployment_ui_base_url == ""`` omits every UI link (link omitted, never a
    broken link); the GCS run.log link only needs ``log_bucket`` + ``vm_name``.
    Returns None when no link could be built.
    """
    base = deployment_ui_base_url.rstrip("/")
    links: list[str] = []

    vm_name = details.get("vm_name") or details.get("vm")
    if base and vm_name:
        links.append(f"<{base}/ops/vms/{vm_name}|VM logs>")
        links.append(f"<{base}/deployments/{vm_name}|Deployment>")

    # An emitter-supplied explicit run.log deep-link (the GCS-console object URL
    # the exit-code monitor attaches) takes precedence — it points straight at
    # the durable run.log object, not the parent folder. Falls back to the
    # bucket/vm-logs folder link when only ``log_bucket`` + ``vm_name`` are known.
    log_url = details.get("log_url")
    if isinstance(log_url, str) and log_url:
        links.append(f"<{log_url}|run.log>")
    elif log_bucket and vm_name:
        gcs_console = f"https://console.cloud.google.com/storage/browser/{log_bucket}/vm-logs/{vm_name}"
        links.append(f"<{gcs_console}|run.log>")

    asset_group = details.get("asset_group")
    if base and asset_group:
        service = details.get("service") or details.get("data_type") or asset_group
        links.append(f"<{base}/service/{service}/data-status?asset_group={asset_group}|Data status>")

    if not links:
        return None
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "  •  ".join(links)}],
    }


def _build_blocks(
    event_name: str,
    summary: str,
    details: dict[str, object],
    *,
    deployment_ui_base_url: str = "",
    log_bucket: str = "",
) -> list[dict[str, object]]:
    """Build a Block Kit payload mirroring the data-pipeline alert style.

    When ``deployment_ui_base_url`` / ``log_bucket`` are supplied, appends a
    fenced-code trace block (the most-informative ``details`` payload) + a
    context block of click-through deep-links (VM logs / deployment / data
    status / GCS run.log). Both extra blocks are omitted when their inputs are
    absent — a clean event renders exactly the prior header+summary+fields.
    """
    severity_raw = details.get("severity")
    severity = str(severity_raw) if severity_raw is not None else None
    emoji = _emoji_for(event_name, severity)

    fields: list[dict[str, object]] = [
        {"type": "mrkdwn", "text": f"*Event:*\n`{event_name}`"},
    ]
    if severity is not None:
        fields.append({"type": "mrkdwn", "text": f"*Severity:*\n{severity}"})
    for label, key in (
        ("Asset group", "asset_group"),
        ("Data type", "data_type"),
        ("Source", "source"),
        ("Venue", "venue"),
        ("Bucket", "bucket"),
        ("VM", "vm"),
        ("VM", "vm_name"),
        ("Umbrella", "umbrella"),
        ("Cloud", "cloud"),
        ("Exit code", "exit_code"),
        ("HTTP status", "http_status"),
        ("Endpoint", "endpoint"),
        ("Category", "category"),
        # DP-CATALOG-001: show exactly what was probed + the freshness budget so
        # "(missing)" reads as a diagnosable "probed gs://… ABSENT, budget=Nh"
        # (operator 2026-06-23 — the alert must SHOW what it checked).
        ("Probed path", "probed_path"),
        ("Artifact present", "artifact_present"),
        ("Budget (h)", "budget_hours"),
        # DP-VM-002 / rate-limit: WHY a flat-captured run was flat (run.log-derived).
        ("No-capture reason", "no_capture_reason"),
    ):
        value = details.get(key)
        if value not in (None, ""):
            fields.append({"type": "mrkdwn", "text": f"*{label}:*\n{value}"})

    blocks: list[dict[str, object]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Data-Pipeline Alert"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
    ]

    explain_block = _build_explain_block(event_name, details)
    if explain_block is not None:
        blocks.append(explain_block)

    blocks.append({"type": "section", "fields": fields})

    trace_block = _build_trace_block(details)
    if trace_block is not None:
        blocks.append(trace_block)

    action_block = _build_action_block(details, deployment_ui_base_url, log_bucket)
    if action_block is not None:
        blocks.append(action_block)

    return blocks


def send_data_pipeline_alert(
    webhook_url: str,
    event_name: str,
    summary: str,
    details: dict[str, object],
    *,
    deployment_ui_base_url: str = "",
    log_bucket: str = "",
) -> bool:
    """Mirror a data-pipeline alert to the #data-pipeline-alerts Slack channel.

    Args:
        webhook_url: Incoming Webhook URL for #data-pipeline-alerts. Empty → no-op.
        event_name: Canonical DP_* event name (e.g. "DP_UNPROVEN_HONEST_ABSENCE").
        summary: Pre-formatted one-line alert summary.
        details: Alert payload — used to enrich the Block Kit fields (asset_group,
            data_type, source, venue, bucket, vm, severity) + the inline trace
            block (evidence / error_message / run_log_tail) + the deep-link
            actions (vm_name / asset_group).
        deployment_ui_base_url: deployment-ui base URL for click-through links;
            empty → links omitted (no broken link).
        log_bucket: GCS bucket holding the durable run.log; empty → run.log link omitted.

    Returns:
        True when Slack accepted the message (HTTP 2xx); False on no-op or failure.
        Never raises — the data-pipeline gate / VM / watcher must not depend on
        this mirror, and CRITICAL events page separately via the incident path.
    """
    if not webhook_url:
        return False

    payload: dict[str, object] = {
        "text": summary,
        "blocks": _build_blocks(
            event_name,
            summary,
            details,
            deployment_ui_base_url=deployment_ui_base_url,
            log_bucket=log_bucket,
        ),
    }

    for attempt in range(3):
        if attempt > 0:
            time.sleep(_BACKOFF_SECS[attempt - 1])
        try:
            response = httpx.post(webhook_url, json=payload, timeout=5.0)
        except httpx.HTTPError as exc:
            logger.warning("data-pipeline-alerts Slack mirror failed (attempt %d): %s", attempt + 1, exc)
            continue
        if response.status_code < 300:
            # Observability (P2 2026-06-23): the success path emitted only an
            # event-sink ``log_event`` (GCS/PubSub), nothing to STDOUT — so the
            # webhook POST was invisible in Cloud Logging. Add an INFO so the
            # consume→route→webhook-POST trace is observable end-to-end.
            logger.info(
                "data-pipeline-alerts Slack POST ok (status %d) event=%s",
                response.status_code,
                event_name,
            )
            log_event(
                "SLACK_MESSAGE_SENT",
                details={"event_name": event_name, "channel": "data-pipeline-alerts"},
            )
            return True
        if response.status_code < 500:
            # 4xx — bad webhook / payload; retrying will not help.
            logger.error(
                "data-pipeline-alerts Slack webhook rejected (status %d): %s",
                response.status_code,
                response.text,
            )
            return False
        logger.warning(
            "data-pipeline-alerts Slack webhook 5xx (status %d, attempt %d)",
            response.status_code,
            attempt + 1,
        )

    logger.error("data-pipeline-alerts Slack mirror exhausted retries for event %s", event_name)
    return False
