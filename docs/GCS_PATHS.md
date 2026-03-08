# Alerting Service — GCS Paths

## Current GCS Usage

The alerting-service is a real-time event router. It does not currently read or write GCS data as part of its core routing loop. All persistent state is held in the in-memory `AlertStore` (capped at 1000 events, trimmed to 500 on overflow). GCS is used only indirectly via the `UnifiedCloudConfig` base class for project resolution.

## Intended GCS Paths (Not Yet Implemented)

When alert history persistence is added, the following path conventions apply. All paths follow the workspace-wide `PATH_REGISTRY` pattern used by services that use `unified-domain-client`.

### Bucket Naming

```
gs://{gcs_bucket_prefix}-{gcp_project_id}/
```

where `gcs_bucket_prefix` defaults to `alerting-service`.

### Path Templates

| Dataset       | Path Pattern                          | Notes                                          |
| ------------- | ------------------------------------- | ---------------------------------------------- |
| Alert history | `alerting/history/date={YYYY-MM-DD}/` | Parquet, partitioned by date                   |
| Alert configs | `alerting/configs/`                   | YAML rule snapshots for audit                  |
| Alert state   | `alerting/state/`                     | Cooldown state for cross-instance coordination |

### Example Resolved Paths

```
gs://alerting-service-my-project-id/alerting/history/date=2026-03-08/
gs://alerting-service-my-project-id/alerting/configs/default_rules.yaml
gs://alerting-service-my-project-id/alerting/state/cooldowns.json
```

## Secret Manager (Not GCS)

The alerting-service reads credentials from Secret Manager, not GCS:

- `alerting-pagerduty-routing-key` — PagerDuty routing key
- `alerting-slack-webhook-url` — Slack Incoming Webhook URL

Secret Manager paths follow the GCP convention: `projects/{project_id}/secrets/{secret_name}/versions/latest`.

## No Direct GCS SDK Imports

Per workspace architecture rules, this service does not import `google.cloud.storage` directly. Any future GCS reads/writes must go through `get_storage_client()` from `unified_cloud_interface`.
