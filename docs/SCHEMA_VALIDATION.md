# Alerting Service — Schema Validation

## Alert Event Schema (`AlertEvent`)

`AlertEvent` is defined in `unified-internal-contracts` and is the canonical Pydantic model for alert payloads flowing through this service.

Key fields (from `unified_internal_contracts.AlertEvent`):

| Field          | Type        | Description                                             |
| -------------- | ----------- | ------------------------------------------------------- |
| `alert_id`     | str         | Unique alert identifier                                 |
| `rule_id`      | str         | Identifier matching a rule in `default_rules.yaml`      |
| `message`      | str         | Human-readable alert message                            |
| `severity`     | str         | One of: `DEBUG`, `INFO`, `WARNING`, `CRITICAL`, `FATAL` |
| `metric_value` | float       | Observed metric value that triggered the rule           |
| `threshold`    | float       | Rule threshold that was breached                        |
| `triggered_at` | datetime    | UTC timestamp of the alert                              |
| `strategy_id`  | str \| None | Strategy that produced the triggering metric            |
| `venue`        | str \| None | Trading venue associated with the alert                 |

Pydantic validation is performed automatically when `AlertEvent` is instantiated. Alerts with missing required fields will fail validation and log a `VALIDATION_FAILED` event.

## PubSub Message Schema

Messages arriving on the PubSub subscriptions are JSON-encoded dictionaries. The subscriber extracts the canonical `event_name` from the following keys (in priority order):

1. `event_name`
2. `event_type`
3. `type`

If none of these keys are present, the event is classified as `UNKNOWN_EVENT` and routed to Slack via the operational fallback. Malformed payloads (non-UTF-8 bytes or invalid JSON) are classified as `MALFORMED_EVENT` and logged with a warning — the subscriber loop never crashes.

Example valid PubSub payload:

```json
{
  "event_name": "CIRCUIT_BREAKER_OPEN",
  "venue": "binance",
  "correlation_id": "abc123",
  "source": "execution-service",
  "message": "Circuit breaker opened after 5 consecutive failures"
}
```

## Slack Block Kit Payload Schema

`slack_dispatcher.build_slack_blocks()` produces Slack Block Kit `attachments` with the following structure:

- **Header block**: `[{severity}] {message}`
- **Section block with fields**: rule_id, metric_value (4 decimal places), threshold, strategy_id, venue, triggered_at (ISO 8601)
- **Actions block**: "View Dashboard" button linking to `{dashboard_url}/system-health`
- **Color coding**: grey (DEBUG), blue (INFO), amber (WARNING), red (CRITICAL), purple (FATAL)

## PagerDuty Payload Schema

`notifiers/pagerduty.send_event()` constructs PagerDuty Events API v2 payloads:

```json
{
  "routing_key": "<from Secret Manager>",
  "event_action": "trigger",
  "payload": {
    "summary": "[EVENT_NAME] {details.message}",
    "severity": "critical",
    "source": "{details.source or 'alerting-service'}",
    "custom_details": { ... }
  }
}
```

`severity` is constrained to the `PagerDutySeverity` literal type: `"critical"`, `"error"`, `"warning"`, or `"info"`. The routing rules in `router.py` always use `"critical"` for the events that reach PagerDuty.

## Alerting Rules Schema (`default_rules.yaml`)

Each rule entry in `default_rules.yaml` conforms to:

```yaml
rule_id: string # Unique identifier
name: string # Human-readable description
metric_name: string # Metric key to evaluate
condition: gt|lt|eq # Comparison operator
threshold: float # Breach threshold
severity: WARNING|CRITICAL|FATAL
channels: [SLACK, PAGERDUTY, EMAIL, UI] # One or more
cooldown_seconds: int # Minimum time between firings of the same rule
```

The 10 built-in rules cover: `reconciliation_drift_usd`, `circuit_breaker_state`, `feature_staleness_seconds`, `dead_letter_queue_depth`, `gcs_write_latency_seconds`, `order_fill_rate_pct`, `trade_execution_latency_seconds`, `pnl_drawdown_pct`, `ibkr_gateway_connected`, `position_notional_usd`.

## Validation at Config Load

`AlertingSystemConfig` is a Pydantic model. Validation runs at instantiation:

- `gcp_project_id` must be a non-empty string (inherited from UnifiedCloudConfig)
- `email_smtp_port` must be an int (defaults to 587)
- `poll_interval_seconds` must be an int (defaults to 10)
- `anthropic_api_key` is optional (None skips AI triage)
