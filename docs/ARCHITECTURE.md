# Alerting Service — Architecture

## Purpose

The alerting-service is the system-wide multi-channel alert dispatcher for the unified trading system. It subscribes to PubSub topics carrying trading and system health events, routes each event to the appropriate notification channel (Slack, PagerDuty, or both), and optionally generates AI-assisted triage notes via Claude. It also exposes a real-time HTTP SSE stream of recent alerts and a REST endpoint for alert history.

## Role in Trading System

- **Depends on**: unified-events-interface (lifecycle/service events), unified-config-interface (UnifiedCloudConfig), unified-internal-contracts (AlertEvent), unified-trading-library (GracefulShutdownHandler, PubSubEventSink, setup_tracing, start_memory_watchdog), unified-cloud-interface (get_queue_client)
- **Intended consumers**: Operators, on-call engineers (via PagerDuty and Slack), trading desks (via UI SSE stream)
- **Upstream**: PubSub subscriptions: `risk_alerts_circuit_breaker_triggers`, `balance_discrepancy_alerts`, `order_rejection_spikes`; coordination events from execution-service (KILL_SWITCH_ACTIVATED, CIRCUIT_BREAKER_OPEN)

## Package Structure

```
alerting_service/
├── main.py                  Entry point: wires config, event sink, subscriber, shutdown
├── config.py                AlertingSystemConfig (extends UnifiedCloudConfig)
├── subscribers/
│   └── alert_subscriber.py  AlertSubscriber: PubSub polling loop, round-robin across subscriptions
├── notifiers/
│   ├── router.py            route_event(): dispatches events to PagerDuty and/or Slack
│   ├── pagerduty.py         PagerDuty Events API v2 notifier (httpx, Secret Manager)
│   └── slack.py             Slack Incoming Webhook notifier (httpx, Secret Manager)
├── core/
│   ├── alert_store.py       In-memory AlertStore: cooldown tracking, recent history, SSE pub/sub
│   ├── slack_dispatcher.py  build_slack_blocks(): Block Kit payload builder; send_slack_alert()
│   ├── claude_slack_agent.py  AI triage via Claude claude-3-5-haiku (Anthropic SDK)
│   └── default_rules.yaml   10 default alerting rules (thresholds, channels, cooldowns)
└── api/
    ├── main.py              FastAPI app setup
    └── routes/
        ├── alerts.py        GET /stream/alerts (SSE), GET /rules/recent
        └── health.py        Health check endpoint
```

## Data Flow

```
PubSub subscriptions
  risk_alerts_circuit_breaker_triggers
  balance_discrepancy_alerts
  order_rejection_spikes
        │
        ▼
AlertSubscriber.stream()          ← polls each subscription via get_queue_client()
        │  JSON deserialization, event_name extraction, correlation_id injection
        ▼
route_event(event_name, details)  ← notifiers/router.py
        │
        ├──► PagerDuty (Events API v2)    KILL_SWITCH_ACTIVATED, CIRCUIT_BREAKER_OPEN
        └──► Slack (Incoming Webhook)     KILL_SWITCH_ACTIVATED, PREFLIGHT_FAILED,
                                          SERVICE_DEGRADED, all other events
```

## Routing Rules

| Event                   | PagerDuty | Slack                      |
| ----------------------- | --------- | -------------------------- |
| `KILL_SWITCH_ACTIVATED` | critical  | yes (belt-and-braces)      |
| `CIRCUIT_BREAKER_OPEN`  | critical  | no                         |
| `PREFLIGHT_FAILED`      | no        | yes                        |
| `SERVICE_DEGRADED`      | no        | yes                        |
| Any other event         | no        | yes (operational fallback) |

Delivery failures are logged via `log_event("ALERT_FAILED")` and never crash the loop.

## Default Alerting Rules (`default_rules.yaml`)

Ten rules covering: reconciliation drift, circuit breaker state, feature staleness, DLQ depth, GCS write latency, order fill rate, execution latency, PnL drawdown, IB Gateway connectivity, and position notional breach. Each rule specifies `metric_name`, `condition` (gt/lt/eq), `threshold`, `severity` (WARNING/CRITICAL/FATAL), notification `channels`, and `cooldown_seconds`.

## AI Triage (claude_slack_agent.py)

When `anthropic_api_key` is configured, `post_ai_triage()` calls Claude claude-3-5-haiku-20241022 with alert context (rule_id, metric_value, threshold, strategy, venue, time) and generates a root-cause hypothesis, immediate action steps, and which service/log to check first. Output is intended to be threaded under the original Slack alert.

## Modes

- **live** (default operational mode): `AlertSubscriber` runs indefinitely, polling subscriptions in a round-robin async loop. Controlled by `GracefulShutdownHandler` (SIGTERM/SIGINT).
- **batch**: Accepted by the CLI parser but routes through the same live subscriber logic.

## API Endpoints

- `GET /stream/alerts` — SSE stream of `AlertEvent` JSON payloads; 30-second heartbeat when idle.
- `GET /rules/recent` — Returns last 100 `AlertEvent` objects from the in-memory `AlertStore`.

## Dependencies

- **Libraries**: unified-events-interface, unified-config-interface, unified-internal-contracts, unified-trading-library, unified-cloud-interface
- **Runtime**: httpx (notifiers), aiohttp (slack_dispatcher), fastapi+uvicorn (API), sse-starlette (SSE), anthropic (AI triage), pyyaml (rules), pydantic (config)
- **Secrets in Secret Manager**: `alerting-pagerduty-routing-key`, `alerting-slack-webhook-url`
