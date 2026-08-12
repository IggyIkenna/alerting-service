"""Alert event subscriber for alerting-service.

Subscribes to the following PubSub topics via get_queue_client():
  - risk_alerts_circuit_breaker_triggers
  - balance_discrepancy_alerts
  - order_rejection_spikes
  - service_error_events

Also processes coordination events emitted by execution-service:
  - KILL_SWITCH_ACTIVATED
  - CIRCUIT_BREAKER_OPEN

SERVICE_ERROR events are dispatched to handle_service_error() which feeds
them into the circuit breaker for error-rate tracking and emits CIRCUIT_OPEN
events when the threshold is exceeded.

On each message the event name is extracted and forwarded to route_event()
which dispatches to PagerDuty and/or Slack according to routing rules.

No direct google.cloud.pubsub_v1 imports — all PubSub access goes
through get_queue_client() from unified_trading_library.cloud_interface.

Lifecycle event taxonomy (common events — alerting-service is a router, not a pipeline):
  SERVICE_EVENT: VALIDATION_STARTED
  SERVICE_EVENT: VALIDATION_COMPLETED
  SERVICE_EVENT: VALIDATION_FAILED
  SERVICE_EVENT: DATA_INGESTION_STARTED
  SERVICE_EVENT: DATA_INGESTION_COMPLETED
  SERVICE_EVENT: PROCESSING_STARTED
  SERVICE_EVENT: PROCESSING_COMPLETED
  SERVICE_EVENT: PERSISTENCE_STARTED
  SERVICE_EVENT: PERSISTENCE_COMPLETED
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from functools import lru_cache
from typing import ClassVar, cast

from unified_trading_library import QueueClient, get_queue_client, run_lifecycle, setup_events

from ..cefi_ml_event_handler import (
    CEFI_ML_EVENT_NAMES,
    handle_cefi_ml_event,
)
from ..config import AlertingSystemConfig
from ..config_reloaders import get_paging_credentials
from ..defi_feature_event_handler import (
    DEFI_FEATURE_EVENT_NAMES,
    handle_defi_feature_event,
)
from ..dr_event_handler import (
    handle_circuit_breaker_fire_payload,
    handle_kill_switch_armed_payload,
    handle_kill_switch_disarm_payload,
)
from ..error_event_handler import handle_service_error
from ..metrics import PROCESSING_LATENCY, RECORDS_PROCESSED
from ..notifiers.router import route_event
from ..notifiers.uts_live_alerts_slack import send_uts_live_alert
from ..recon_drift_event_handler import (
    handle_batch_live_recon_drift_payload,
    handle_batch_vs_live_recon_drifted_payload,
)
from ..risk_rule_event_handler import handle_risk_rule_fired_payload
from ..rules.consolidator_rules import (
    handle_consolidator_down_payload,
    handle_consolidator_recovered_payload,
    handle_manifest_consolidation_failed_payload,
)
from ..rules.margin_rules import route_margin_event_payload

logger = logging.getLogger(__name__)

_events_initialised = False


def _ensure_local_events() -> None:
    """Initialise UTL event logging in LOCAL mode (idempotent, process-wide).

    The router's telemetry calls (``log_event("ALERT_ROUTED"/"ALERT_SENT"/...)``)
    REQUIRE event logging to be initialised — otherwise the FIRST routed message
    raises ``RuntimeError("Event logging not initialized")``, which crashed
    message processing and silently killed the background pull task (the
    2026-06-22 "zero alerts": the Cloud Run subscriber died on its first DP_*
    message). LOCAL (not LIVE) is MANDATORY here: this subscriber *consumes* the
    ``lifecycle-events`` topic, so a LIVE sink would self-PUBLISH those telemetry
    events back onto it → re-consume → feedback loop. Slack delivery happens via
    the webhook (HTTP) in the router, independent of the event sink — so LOCAL
    (stdout-only) loses nothing. ``sink=None`` is explicit (local mode publishes
    nowhere). SSOT: plans/active/issues/dp_event_pubsub_delivery_gap_2026_06_22.md.
    """
    global _events_initialised
    if _events_initialised:
        return
    setup_events(service_name="dp-alerting-subscriber", mode="local", sink=None)
    _events_initialised = True


# PubSub subscriptions to pull from.
# margin-events: canonical UAC topic, single producer = position-balance-monitor.
_ALERT_SUBSCRIPTIONS: tuple[str, ...] = (
    "risk_alerts_circuit_breaker_triggers",
    "balance_discrepancy_alerts",
    "order_rejection_spikes",
    "service_error_events",
    "margin-events",
    # DeFi data-quality alerts emitted by the MTDS subgraph_health_probe
    # cron (6h cadence). Topic provisioned in
    # deployment-service/terraform/gcp/subgraph_health_probe_scheduler.tf.
    # Event kinds: SUBGRAPH_SILENT_DATA_LOSS / SUBGRAPH_PROBE_FAILED. The
    # generic router.route_event() dispatches to PagerDuty/Slack based on
    # event_name == "SUBGRAPH_SILENT_DATA_LOSS"; no dedicated handler is
    # required for the May-23 cutover (kind alone is sufficient).
    "defi_data_quality_alerts",
    # lifecycle-events: the canonical UTL `log_event` live-mode topic
    # (PubSubEventSink default). Carries the data-pipeline DP_* family
    # (data_pipeline_hardening_self_monitoring_2026_06_22 — daily digest /
    # hygiene / re-probe crons + the deployment-service fleet monitors via
    # _ensure_live_events) AND CONSOLIDATOR_DOWN / CONSOLIDATOR_RECOVERED.
    # route_event() exact-matches DP_* against the UAC DATA_PIPELINE_ALERT_RULES
    # registry → _route_data_pipeline_event → #data-pipeline-alerts. Without
    # this subscription DP_* / CONSOLIDATOR_DOWN are published but never
    # consumed → never reach Slack (the delivery gap closed 2026-06-22; see
    # plans/active/issues/dp_event_pubsub_delivery_gap_2026_06_22.md).
    "lifecycle-events-sub",
    # service-lifecycle-events: the OTHER, UAC-canonical lifecycle topic
    # (InternalPubSubTopic.SERVICE_EVENTS, unified-api-contracts/internal/
    # pubsub.py) — live-mode STARTED/STOPPED/FAILED events publish here since
    # unified-trading-library@9bdcf7a2, but had zero subscribers until this
    # entry (operator ruling 2026-07-29). Kept alongside lifecycle-events-sub
    # rather than replacing it — the two topics carry different event
    # populations today; reconciling to one topic is a separate follow-up.
    # See plans/active/issues/live_mode_event_sink_topic_missing_2026_06_21.md.
    "service-lifecycle-events-sub",
)

# Topics with NO publisher in the codebase as of 2026-05-29 audit
# (cefi_venue_backfill_coverage_remediation §6G). These subscriptions exist in
# GCP and in _ALERT_SUBSCRIPTIONS but nothing publishes to them yet — expected
# to see zero messages until publishers are implemented. Listed explicitly so
# "zero traffic for 5+ days" is a known gap, not an unknown incident.
_SUBSCRIPTIONS_WITH_NO_PUBLISHER: frozenset[str] = frozenset(
    {
        "risk_alerts_circuit_breaker_triggers",  # planned: execution-service circuit breaker
        "balance_discrepancy_alerts",  # planned: batch-live-reconciliation-service
        "order_rejection_spikes",  # planned: execution-service order gateway
        "service_error_events",  # planned: cross-service error aggregation bus
    }
)

# Coordination events from execution-service that must also be routed.
_COORDINATION_EVENTS: frozenset[str] = frozenset({"KILL_SWITCH_ACTIVATED", "CIRCUIT_BREAKER_OPEN"})

# Events that have dedicated handlers (not just generic routing).
_SERVICE_ERROR_EVENT: str = "SERVICE_ERROR"
_MARGIN_EVENT: str = "MarginEvent"
# Disaster-recovery + risk-rule typed events (Phase 5.B risk plan / Phase 4.C DR plan).
_RISK_RULE_FIRED_EVENT: str = "RiskRuleFiredEvent"
_KILL_SWITCH_ARMED_EVENT: str = "KillSwitchArmedEvent"
_KILL_SWITCH_DISARM_EVENT: str = "KillSwitchDisarmEvent"
_CIRCUIT_BREAKER_FIRED_EVENT: str = "CircuitBreakerFired"
_BATCH_VS_LIVE_RECON_DRIFTED_EVENT: str = "BATCH_VS_LIVE_RECON_DRIFTED"
_BATCH_LIVE_RECON_DRIFT_EVENT: str = "BATCH_LIVE_RECON_DRIFT"
# Manifest-consolidator liveness events (UTL consolidator_liveness watchdog +
# manifest_consolidator) — see rules/consolidator_rules.py.
_CONSOLIDATOR_DOWN_EVENT: str = "CONSOLIDATOR_DOWN"
_MANIFEST_CONSOLIDATION_FAILED_EVENT: str = "MANIFEST_CONSOLIDATION_FAILED"
_CONSOLIDATOR_RECOVERED_EVENT: str = "CONSOLIDATOR_RECOVERED"


def _extract_event_name(payload: dict[str, object]) -> str:
    """Extract the canonical event_name from a deserialized PubSub payload.

    Looks for ``event``, ``event_name``, ``event_type``, ``type``, or ``kind``
    keys in that order. Falls back to ``"UNKNOWN_EVENT"`` when none are present.

    ``event`` is FIRST + REQUIRED: it is the canonical key the UTL
    ``PubSubEventSink.write_event`` publishes the event name under
    (``{"event": name, "service": ..., "metadata": {...}}`` — see
    unified-trading-library/unified_trading_library/event_sink.py). Every UTL
    ``log_event`` on the ``lifecycle-events`` topic (all ``DP_*`` data-pipeline
    alerts + ``CONSOLIDATOR_DOWN``) serializes the name to ``event``. Omitting it
    here mis-extracts every such event to ``UNKNOWN_EVENT`` → no rule match →
    silently DROPPED before reaching ``#data-pipeline-alerts`` (root-caused
    2026-06-22: the subscriber was attached to ``lifecycle-events-sub`` + pulling
    messages but every ``DP_*`` fell through to ``UNKNOWN_EVENT``). SSOT:
    plans/active/issues/dp_event_pubsub_delivery_gap_2026_06_22.md.
    ``kind`` is the canonical field on subgraph_health_probe alerts
    (defi_data_quality_alerts topic) so probe alerts route cleanly without each
    probe having to pre-canonicalise to event_name.
    """
    for key in ("event", "event_name", "event_type", "type", "kind"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "UNKNOWN_EVENT"


def _unwrap_utl_envelope(payload: dict[str, object]) -> dict[str, object]:
    """Flatten the UTL ``log_event`` envelope into a FLAT details dict.

    Root cause of the generic-alert bug (operator escalation 2026-06-23): every
    ``log_event`` on the ``lifecycle-events`` topic is published by
    ``PubSubEventSink.write_event`` as::

        {"event": <name>, "service": <svc>,
         "metadata": {"timestamp": ..., "service_name": ..., "severity": ...,
                      "details": {<the emitter's real payload>},
                      "correlation_id": ...}}

    i.e. the emitter's ``log_event(..., details={vm_name, exit_code,
    error_message, run_log_tail, umbrella, asset_group, ...})`` lands TWO levels
    deep at ``payload["metadata"]["details"]``, and ``severity`` lands at
    ``payload["metadata"]["severity"]``. The subscriber previously routed the
    RAW top-level dict as ``details``, so every ``details.get("vm_name")`` /
    ``.get("exit_code")`` / ``.get("severity")`` / ``.get("umbrella")`` the
    router + the ``data_pipeline_slack`` formatter look up returned ``None`` →
    the delivered Slack alert was generic (event name + nothing actionable).

    This lifts the envelope so the rich payload is at the TOP level the router /
    formatter read:

      - ``metadata["details"]`` keys → top level (vm_name, exit_code,
        error_message, run_log_tail, umbrella, asset_group, message, …);
      - ``metadata["severity"]`` → top-level ``severity`` (the field-renderer +
        the emoji + the CRITICAL→PagerDuty gate key off it);
      - ``metadata["correlation_id"]`` → top-level ``correlation_id`` for tracing.

    A FLAT legacy payload (kill-switch / margin / risk-rule emitters that
    publish a flat dict with no ``metadata`` envelope) is returned UNCHANGED —
    the unwrap only fires on the canonical ``{event|event_name, metadata:{…}}``
    shape, so the typed handlers that read those flat payloads are untouched.
    SSOT: plans/active/data_completion_to_100_all_ag_2026_06_21.md (alerting
    verbosity items).
    """
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return payload
    meta = cast("dict[str, object]", metadata)
    flat: dict[str, object] = {}
    inner = meta.get("details")
    if isinstance(inner, dict):
        flat.update(cast("dict[str, object]", inner))
    # Promote the envelope-level fields the router/formatter read at top level.
    # These never clobber a same-named key the emitter put in ``details`` — the
    # inner payload wins (it is the more-specific value).
    for env_key in ("severity", "correlation_id", "service_name", "timestamp"):
        env_val = meta.get(env_key)
        if env_val not in (None, "") and env_key not in flat:
            flat[env_key] = env_val
    return flat


@lru_cache(maxsize=1)
def _get_subscriber_alerting_config() -> AlertingSystemConfig:
    """Return singleton AlertingSystemConfig instance (mirrors router._get_cloud_config)."""
    return AlertingSystemConfig()


def _page_own_dispatch_failure(subscription: str, event_name: str | None, error: Exception) -> None:
    """Best-effort direct page when ``_route_one`` fails to dispatch an alert.

    Mirrors the ``notify_daily_summary_failed`` "a dead digest must not be
    silent" pattern (``codex/04-architecture/agent-orchestrator-alerting.md``):
    a broken alert route must be loud about ITS OWN failure, not just
    ``logger.warning``-and-skip — otherwise a downstream fault (missing
    PagerDuty/Telegram secret, a misconfigured rule, an unhandled payload
    shape) silently eats a page with no escalation of that failure itself.
    Found while investigating why the 2026-07-15
    consolidator-liveness-watchdog exit(1) failures never paged; see
    plans/active/issues/defi_consolidator_cron_left_paused_2026_07_15.md.

    Deliberately bypasses ``dispatch_event``/``route_event`` — the very path
    that just failed — and posts directly via the ``#uts-live-alerts``
    webhook, which needs no routing rules, dedup state, or typed-handler
    success. Never raises: an unconfigured webhook or a Slack-side failure
    here must not take down the subscriber loop that is already recovering
    from the original error.
    """
    try:
        config = _get_subscriber_alerting_config()
        sm_creds = get_paging_credentials()
        webhook = sm_creds.get("uts_live_alerts_slack_webhook") or config.uts_live_alerts_slack_webhook
        if not webhook:
            logger.error(
                "alert dispatch FAILED on subscription=%s event=%s and could NOT be paged "
                "(no uts-live-alerts webhook configured): %s",
                subscription,
                event_name or "UNKNOWN",
                error,
            )
            return
        send_uts_live_alert(
            webhook,
            "ALERT_DISPATCH_FAILED",
            f"Alert dispatch failed on subscription `{subscription}` (event={event_name or 'UNKNOWN'}): {error}",
            {
                "subscription": subscription,
                "failed_event_name": event_name or "UNKNOWN",
                "error": str(error),
                "severity": "critical",
            },
        )
    except Exception as _page_err:
        logger.exception(
            "_route_one: failed to page its own dispatch failure (subscription=%s, event=%s): %s",
            subscription,
            event_name or "UNKNOWN",
            _page_err,
        )


def _deserialize_message(data: bytes) -> tuple[str, dict[str, object]]:
    """Deserialize raw PubSub bytes into (event_name, details).

    Unwraps the UTL ``log_event`` envelope (``{event, metadata:{severity,
    details}}``) into a FLAT details dict so the router + the
    ``data_pipeline_slack`` formatter see vm_name / exit_code / error_message /
    run_log_tail / severity / umbrella at the TOP level — see
    ``_unwrap_utl_envelope`` for the root-cause + the flat-legacy-payload
    pass-through.

    Returns ("MALFORMED_EVENT", {}) on decode/parse failure so the caller
    can still log the anomaly without crashing the subscriber loop.
    """
    try:
        payload: dict[str, object] = cast(dict[str, object], json.loads(data.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Failed to decode alert message payload: %s", exc)
        return "MALFORMED_EVENT", {}

    event_name = _extract_event_name(payload)
    return event_name, _unwrap_utl_envelope(payload)


class AlertSubscriber:
    """Pulls alert events from multiple PubSub subscriptions and routes them.

    Uses get_queue_client() from unified_trading_library.cloud_interface — no direct PubSub SDK.

    Each subscription is polled in a round-robin fashion inside a single async
    loop so that all topics share one event loop iteration.

    Usage::

        subscriber = AlertSubscriber(project_id="my-project")
        async for event_name, details in subscriber.stream():
            # route_event is called internally; this exposes the pair
            # for observability / testing hooks if needed.
            pass

        # Or simply run until shutdown:
        await subscriber.run_until_stopped()
    """

    def __init__(
        self,
        project_id: str,
        subscriptions: tuple[str, ...] = _ALERT_SUBSCRIPTIONS,
        poll_timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self._project_id = project_id
        self._subscriptions = subscriptions
        self._poll_timeout = poll_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._client: QueueClient = get_queue_client(project_id=project_id)
        self._running = False
        logger.info(
            "AlertSubscriber initialized: subscriptions=%s, project=%s",
            self._subscriptions,
            self._project_id,
        )
        no_pub = [s for s in self._subscriptions if s in _SUBSCRIPTIONS_WITH_NO_PUBLISHER]
        if no_pub:
            logger.warning(
                "AlertSubscriber: %d subscription(s) have no publisher implemented yet — "
                "zero traffic on these is expected, not an incident: %s. "
                "See _SUBSCRIPTIONS_WITH_NO_PUBLISHER in alert_subscriber.py for context.",
                len(no_pub),
                no_pub,
            )

    def stop(self) -> None:
        """Signal the stream loop to stop after the current poll cycle."""
        self._running = False

    def _process_message(
        self,
        data: bytes,
        attrs: dict[str, str],
        subscription: str,
    ) -> tuple[str, dict[str, object]]:
        """Deserialize one PubSub message and enrich it with tracing metadata.

        Returns (event_name, enriched_details) ready to route and yield.
        """
        event_name, details = _deserialize_message(data)
        # correlation_id precedence: the UTL envelope value (lifted into ``details``
        # from ``metadata.correlation_id`` by ``_unwrap_utl_envelope``) → a flat
        # top-level value → a fresh uuid. The raw ``metadata`` envelope carries it
        # nested, so reading the raw top level alone would always miss it.
        correlation_id = str(details.get("correlation_id") or uuid.uuid4())
        enriched: dict[str, object] = {
            **details,
            "correlation_id": correlation_id,
            "source": attrs.get("source", subscription),
        }
        # Observability breadcrumb only — a plain log line, NOT a published event.
        # This was previously ``log_event("ALERT_RECEIVED", ...)``, which (a) RAISED
        # ``RuntimeError("Event logging not initialized")`` because the subscriber
        # never initialised event logging — crashing message processing on the FIRST
        # message and silently killing the background pull task (the 2026-06-22 "zero
        # alerts": the Cloud Run subscriber went dead on its first DP_* message); and
        # (b) if "fixed" with a LIVE sink it would self-PUBLISH back to the
        # ``lifecycle-events`` topic the subscriber itself consumes → ALERT_RECEIVED
        # feedback loop. ALERT_RECEIVED is pure telemetry (nothing routes on it), so a
        # logger line is the correct, loop-safe form. SSOT:
        # plans/active/issues/dp_event_pubsub_delivery_gap_2026_06_22.md.
        logger.info(
            "ALERT_RECEIVED event_name=%s subscription=%s correlation_id=%s",
            event_name,
            subscription,
            correlation_id,
        )
        return event_name, enriched

    # Closed-set typed-event handler dispatch (each handler signature:
    # ``payload: dict[str, object]) -> None``). Kept at module-level via a
    # tuple to satisfy ruff C901 (dispatch_event complexity ≤ 7).
    _TYPED_HANDLERS: ClassVar[dict[str, Callable[[dict[str, object]], None]]] = {
        _SERVICE_ERROR_EVENT: handle_service_error,
        _MARGIN_EVENT: route_margin_event_payload,
        _RISK_RULE_FIRED_EVENT: handle_risk_rule_fired_payload,
        _KILL_SWITCH_ARMED_EVENT: handle_kill_switch_armed_payload,
        _KILL_SWITCH_DISARM_EVENT: handle_kill_switch_disarm_payload,
        _CIRCUIT_BREAKER_FIRED_EVENT: handle_circuit_breaker_fire_payload,
        _BATCH_VS_LIVE_RECON_DRIFTED_EVENT: handle_batch_vs_live_recon_drifted_payload,
        _BATCH_LIVE_RECON_DRIFT_EVENT: handle_batch_live_recon_drift_payload,
        _CONSOLIDATOR_DOWN_EVENT: handle_consolidator_down_payload,
        _MANIFEST_CONSOLIDATION_FAILED_EVENT: handle_manifest_consolidation_failed_payload,
        _CONSOLIDATOR_RECOVERED_EVENT: handle_consolidator_recovered_payload,
    }

    @classmethod
    def dispatch_event(cls, event_name: str, enriched: dict[str, object]) -> None:
        """Route an event to the appropriate handler.

        SERVICE_ERROR events feed the circuit breaker. ``MarginEvent`` is
        validated against the UAC schema and severity-mapped to the
        ``MARGIN_*`` event-name family before going to the standard router.
        DeFi feature events (FEATURE_*) bridge to the DeFi rule check_*
        functions via ``handle_defi_feature_event``. Everything else falls
        through to the generic router.
        """
        typed_handler = cls._TYPED_HANDLERS.get(event_name)
        if typed_handler is not None:
            typed_handler(enriched)
            return
        if event_name in DEFI_FEATURE_EVENT_NAMES:
            handle_defi_feature_event(event_name, enriched)
            return
        if event_name in CEFI_ML_EVENT_NAMES:
            handle_cefi_ml_event(event_name, enriched)
            return
        route_event(event_name, enriched)

    def _route_one(self, data: bytes, attrs: dict[str, str], subscription: str) -> tuple[str, dict[str, object]] | None:
        """Process + route ONE pulled message.

        Returns the ``(event_name, enriched)`` pair, or ``None`` when the message
        was skipped. Per-message isolation: a single un-routable/malformed message
        must NOT kill the loop (the silent-death class — one raise here previously
        took the whole background pull task down → all alerting offline). Shutdown
        signals still propagate. A dispatch failure is escalated via
        ``_page_own_dispatch_failure`` (not just ``logger.warning``) so a broken
        alert route is itself loud — see that function's docstring.
        """
        _start = time.perf_counter()
        event_name: str | None = None
        try:
            event_name, enriched = self._process_message(data, attrs, subscription)
            self.dispatch_event(event_name, enriched)
            RECORDS_PROCESSED.labels(status="success").inc()
            # Observability (P2 2026-06-23): the success path previously logged
            # NOTHING — only failures warned — so routing was invisible in Cloud
            # Logging. Emit one INFO per consumed+routed message so the
            # consume→route trace is observable. SSOT:
            # plans/active/issues/dp_event_pubsub_delivery_gap_2026_06_22.md.
            logger.info(
                "consumed+routed event=%s sub=%s severity=%s",
                event_name,
                subscription,
                enriched.get("severity", "UNKNOWN"),
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise  # shutdown signals must propagate
        except Exception as _msg_err:
            RECORDS_PROCESSED.labels(status="error").inc()
            PROCESSING_LATENCY.observe(time.perf_counter() - _start)
            logger.warning("alert message processing failed on %s — skipping: %s", subscription, _msg_err)
            _page_own_dispatch_failure(subscription, event_name, _msg_err)
            return None
        PROCESSING_LATENCY.observe(time.perf_counter() - _start)
        return event_name, enriched

    async def stream(self) -> AsyncIterator[tuple[str, dict[str, object]]]:
        """Yield (event_name, details) pairs from all subscribed topics.

        Calls route_event() for each message before yielding, so callers
        receive the pair only after routing has been attempted.

        Skips malformed messages with a warning; never crashes the loop.
        """
        self._running = True
        _ensure_local_events()  # router/telemetry log_event needs an initialised sink (LOCAL — no loop)
        logger.info("Starting alert subscriber stream: %s", self._subscriptions)
        loop = asyncio.get_event_loop()
        try:
            while self._running:
                for subscription in self._subscriptions:
                    if not self._running:
                        break
                    try:
                        batch: list[tuple[bytes, dict[str, str]]] = await loop.run_in_executor(
                            None,
                            lambda sub=subscription: self._client.subscribe_once(sub, timeout=self._poll_timeout),
                        )
                    except Exception as _sub_err:  # one bad subscription must not crash the whole subscriber
                        # A missing/misconfigured subscription (e.g. a 404 NotFound when its
                        # topic exists but the subscription was never provisioned in GCP) must
                        # NOT take down alerting for every OTHER subscription. Log + skip this
                        # round; the loop retries next tick and self-heals once provisioned.
                        # (Incident 2026-06-22: a never-created `defi_data_quality_alerts`
                        # subscription crashed the whole subscriber on startup → all alerting
                        # offline. SSOT: dp_event_pubsub_delivery_gap_2026_06_22.md.)
                        logger.warning("subscribe_once(%s) failed — skipping this round: %s", subscription, _sub_err)
                        continue
                    for data, attrs in batch:
                        routed = self._route_one(data, attrs, subscription)
                        if routed is not None:
                            yield routed

                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.info("AlertSubscriber stream cancelled")
            raise

    async def run_until_stopped(self) -> None:
        """Drive the stream loop until stop() is called or the task is cancelled.

        Wrapped in ``run_lifecycle`` (the canonical service-lifecycle pairing): the
        subscriber emits ``DP_ALERTING_SUBSCRIBER_RUN_STARTED`` on boot and a
        terminal ``_RUN_COMPLETED`` / ``_RUN_FAILED`` on exit. Event logging is
        initialised LOCAL-first (``_ensure_local_events``) so both the lifecycle
        events and the router telemetry ``log_event`` calls have an initialised
        sink and never raise. Consumes all yielded pairs; side-effects happen
        inside stream().
        """
        _ensure_local_events()
        with run_lifecycle("dp-alerting-subscriber"):
            async for _event_name, _details in self.stream():
                pass
