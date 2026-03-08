# Testing

## Quick Start

```bash
# Run all tests (unit + integration) with coverage
pytest tests/ -v

# Run unit tests only
pytest tests/unit/ -v

# Run integration tests only
pytest tests/integration/ -v -m integration

# Run with coverage report
pytest tests/ --cov=alerting_service --cov-report=term-missing --cov-report=html
```

Coverage threshold is enforced at **70%** (`fail_under = 70` in `pyproject.toml`). The build fails if coverage drops below this.

## Test Structure

```
tests/
├── unit/
│   ├── test_config.py             AlertingSystemConfig field defaults and validation
│   ├── test_event_logging.py      log_event() calls in main.py (STARTED, STOPPED, FAILED)
│   ├── test_main.py               main() wiring: config, subscriber, shutdown handler
│   ├── test_alert_store.py        AlertStore: cooldown, record_fired, subscribe/publish, trim
│   ├── test_slack_dispatcher.py   build_slack_blocks() Block Kit payload, send_slack_alert()
│   ├── test_health_and_auth.py    Health endpoint and auth.py
│   ├── notifiers/
│   │   ├── test_router.py         route_event() routing matrix: all five event categories
│   │   ├── test_pagerduty.py      send_event(): HTTP 202 success, non-202 failure, httpx errors
│   │   └── test_slack.py          send_message(): HTTP 200 success, non-200 failure, httpx errors
│   └── subscribers/
│       └── test_alert_subscriber.py  AlertSubscriber: deserialization, enrichment, stream loop
└── integration/
    ├── test_notifiers_integration.py   Router + notifiers wired together (mocked HTTP)
    └── test_router_integration.py      Router end-to-end with mocked PagerDuty/Slack calls
```

## Key Test Patterns

### Notifier Tests (router, pagerduty, slack)

All external HTTP calls are patched using `unittest.mock.patch`. Tests verify:

- Correct notifier is called for each event type (see routing matrix in ARCHITECTURE.md)
- PagerDuty severity is `"critical"` for kill-switch and circuit-breaker events
- Delivery failures return `False` and log `ALERT_FAILED` — they never raise
- Details dict is forwarded verbatim to the notifier payload

```python
# Example: patch PagerDuty and Slack in router tests
with patch("alerting_service.notifiers.router.pd_send_event", return_value=True) as pd:
    with patch("alerting_service.notifiers.router.slack_send_message", return_value=True) as slack:
        route_event("KILL_SWITCH_ACTIVATED", {"strategy": "s1"})
        pd.assert_called_once()
        slack.assert_called_once()
```

### AlertStore Tests

`AlertStore` is pure Python (no GCP dependencies). Tests validate cooldown window logic, the 1000-event cap with 500-event trim, and the `asyncio.Queue`-based SSE pub/sub.

### Subscriber Tests

`AlertSubscriber` is tested with a mocked `QueueClient`. Tests verify JSON deserialization, `MALFORMED_EVENT` handling on bad bytes, correlation_id injection, and that `route_event()` is called for each message.

## Integration Tests

Integration tests are marked `@pytest.mark.integration`. They do not make real network calls — all HTTP is mocked with `unittest.mock`. They test the full router-to-notifier wiring end-to-end.

```bash
pytest tests/integration/ -v -m integration
```

## Running Without GCP Credentials

All unit tests mock GCP/PubSub dependencies. The full suite runs offline with:

```bash
pytest tests/unit/ -v
```

Integration tests also run offline (HTTP is mocked). No `GCP_PROJECT_ID` is required for unit tests; tests that call `UnifiedCloudConfig()` directly should mock the config or set `GCP_PROJECT_ID=test-project` as an env var.

## Type Checking

```bash
basedpyright alerting_service/
```

Uses strict mode (`reportAny`, `reportUnknownMemberType`, `reportUnknownVariableType`, `reportUnknownArgumentType`, `reportUnknownParameterType` all set to `error`).

## Linting

```bash
ruff check alerting_service/ tests/
ruff format --check alerting_service/ tests/
```
