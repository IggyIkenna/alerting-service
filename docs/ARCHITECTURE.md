# Alerting System — Architecture

## Purpose

The alerting-system is intended to provide multi-channel alerting (Slack, email, PagerDuty) for system health and trading events across the unified trading system. The current implementation is a stub: `main.py` sets up event logging and a placeholder for service logic. No Pub/Sub consumers, alert channels, or routing logic are implemented yet.

## Role in Trading System

- **Depends on**: unified-events-interface (lifecycle events), unified-config-interface (UnifiedCloudConfig)
- **Intended consumers**: Operators, on-call engineers, trading desks
- **Intended upstream**: Pub/Sub topics for lifecycle events, risk breaches, trading alerts (not yet wired)

## Data Flow

- **Input**: Not implemented. Intended: Pub/Sub subscriptions for event topics (e.g., risk breaches, service failures, trading events)
- **Output**: Not implemented. Intended: Slack webhooks, email, PagerDuty incidents

## Key Components

- `alerting_system/main.py` — Entry point; sets up events, placeholder for service logic
- `alerting_system/config.py` — AlertingSystemConfig extending UnifiedCloudConfig (service_name only)

No `cli/`, `engine/`, or `adapters/` packages exist. Structure is minimal.

## Modes

- **Batch**: Not implemented
- **Live**: Intended (per README). Current code uses `mode="batch"` in setup_events; logic is unimplemented

## Dependencies

- **Libraries**: unified-events-interface, unified-config-interface
- **Upstream**: None (stub)
- **Downstream**: None (stub)
