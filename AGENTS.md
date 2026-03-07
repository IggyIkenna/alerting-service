# AGENTS.md

## Setup

```bash
uv sync --extra dev
source .venv/bin/activate
```

## Quality Gates

```bash
bash scripts/quality-gates.sh
```

## Type Checking

```bash
timeout 120 basedpyright alerting_service/
```

## Key Entry Points

- `alerting_service/main.py` — main entry point
- `alerting_service/api/` — API layer

## Notes

- Initialize events with `from unified_events_interface import setup_events`
- Required env vars: `GCP_PROJECT_ID` — see `docs/CONFIGURATION.md`
- Contains PagerDuty notifier (`alerting_service/notifiers/pagerduty.py`) — PD Events API v2 via httpx, secret from Secret Manager
- Contains Slack notifier (`alerting_service/notifiers/slack.py`) — Incoming Webhooks via httpx, secret from Secret Manager
- Contains alert router (`alerting_service/notifiers/router.py`): `KILL_SWITCH_ACTIVATED` → PD+Slack; `CIRCUIT_BREAKER_OPEN` → PD; `PREFLIGHT_FAILED` → Slack
- Owns PubSub propagation for circuit breaker state changes
