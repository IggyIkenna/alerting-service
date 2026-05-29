# Configuration

## Config Class

`AlertingSystemConfig` in `alerting_service/config.py` extends `UnifiedCloudConfig` from `unified-config-interface`. All fields are Pydantic model fields. Fields inherited from `UnifiedCloudConfig` (e.g. `gcp_project_id`) are resolved from environment variables by the base class.

## Config Fields

| Field                           | Type                     | Default              | Description                                                                                                                 |
| ------------------------------- | ------------------------ | -------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `gcp_project_id`                | str                      | —                    | GCP project ID (inherited from UnifiedCloudConfig; required)                                                                |
| `service_name`                  | str                      | `"alerting-service"` | Service name used in event logging and PubSub topic name                                                                    |
| `slack_webhook_url`             | str                      | `""`                 | Slack incoming webhook URL (fallback; prefer Secret Manager)                                                                |
| `uts_live_alerts_slack_webhook` | str                      | `""`                 | Slack webhook for `#uts-live-alerts`; live-ops alerts mirror here (env `UTS_LIVE_ALERTS_SLACK_WEBHOOK` fallback; prefer SM) |
| `pagerduty_routing_key`         | str \| None              | `None`               | PagerDuty routing key (fallback; prefer Secret Manager)                                                                     |
| `email_smtp_host`               | str \| None              | `None`               | SMTP host for email alerts (optional, not yet wired in routing)                                                             |
| `email_smtp_port`               | int                      | `587`                | SMTP port (default: 587 / STARTTLS)                                                                                         |
| `email_to`                      | ClassVar[list[str]]      | `[]`                 | Email recipients (class-level; not per-instance)                                                                            |
| `google_oauth_domain`           | str                      | `""`                 | OAuth domain for UI authentication                                                                                          |
| `anthropic_api_key`             | str \| None              | `None`               | Anthropic API key for AI triage via Claude                                                                                  |
| `poll_interval_seconds`         | int                      | `10`                 | Fallback poll interval for metrics polling (seconds)                                                                        |
| `metrics_endpoints`             | ClassVar[dict[str, str]] | `{}`                 | Named metrics endpoint map (class-level)                                                                                    |

## Environment Variables

The following environment variables are read by `UnifiedCloudConfig` (the base class). Set these in Cloud Run, `.env`, or via Secret Manager references:

| Variable            | Required | Description                                                 |
| ------------------- | -------- | ----------------------------------------------------------- |
| `GCP_PROJECT_ID`    | Yes      | GCP project used for PubSub, Secret Manager, and event sink |
| `GCS_BUCKET_PREFIX` | No       | GCS bucket prefix (default: `alerting-service`)             |
| `LOG_LEVEL`         | No       | Python logging level (default: `INFO`)                      |

## Secret Manager Secrets

The notifiers read secrets directly from Secret Manager using `get_secret_client()` from `unified_trading_library`. These are NOT environment variables — they are fetched at runtime per invocation:

| Secret Name                              | Used By                                                                     | Description                                                                                                                                                   |
| ---------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `alerting-pagerduty-routing-key`         | `notifiers/pagerduty.py`                                                    | PagerDuty Events API v2 routing key                                                                                                                           |
| `alerting-slack-webhook-url`             | `notifiers/slack.py`                                                        | Slack Incoming Webhook URL                                                                                                                                    |
| `alerting-uts-live-alerts-slack-webhook` | `notifiers/uts_live_alerts_slack.py` (via `config_reloaders.py` hot-reload) | Slack Incoming Webhook for `#uts-live-alerts`; live-ops alerts (LIVE_ALERT_RULES) mirror here alongside Telegram. Same `agent-orchestrator-alerts` Slack app. |

To provision these secrets:

```bash
echo -n "YOUR_ROUTING_KEY" | gcloud secrets create alerting-pagerduty-routing-key \
  --data-file=- --project=YOUR_PROJECT_ID

echo -n "https://hooks.slack.com/services/..." | gcloud secrets create alerting-slack-webhook-url \
  --data-file=- --project=YOUR_PROJECT_ID

echo -n "https://hooks.slack.com/services/T.../B.../..." | gcloud secrets create alerting-uts-live-alerts-slack-webhook \
  --data-file=- --project=YOUR_PROJECT_ID
```

### UTS Live Alerts → Slack mirror

Live-ops runtime alerts (events matching `LIVE_ALERT_RULES`) are delivered to the Telegram "UTS Live Alerts" group and **mirrored** to the `#uts-live-alerts` Slack channel by `notifiers/uts_live_alerts_slack.py`. The mirror is best-effort: a Slack failure never affects Telegram delivery, and it no-ops when no webhook is configured. CI/QG events are **not** mirrored — they have their own Slack channel via `unified-trading-pm/.github/workflows/notify-slack.yml`. The webhook is hot-reloaded from `alerting-uts-live-alerts-slack-webhook` (SM) with the `UTS_LIVE_ALERTS_SLACK_WEBHOOK` env var as fallback.

## PubSub Event Sink

At startup, `main.py` constructs a `PubSubEventSink` pointed at topic `{service_name}-events` (i.e. `alerting-service-events`) in the configured GCP project. This is the outbound channel for all `log_event()` calls (STARTED, STOPPED, ALERT_SENT, ALERT_FAILED, etc.).

## AlertSubscriber Tuning

The following `AlertSubscriber` constructor parameters can be adjusted programmatically (not yet exposed as config fields):

| Parameter               | Default | Description                           |
| ----------------------- | ------- | ------------------------------------- |
| `poll_timeout_seconds`  | `5.0`   | Timeout per PubSub pull call          |
| `poll_interval_seconds` | `0.1`   | Sleep between round-robin poll cycles |

## ConfigStore

Runtime config persistence via ConfigStore is not yet implemented. When added, the config bucket will follow the pattern `config-store-{gcp_project_id}/alerting-service/`.
