# Alerting Service — GCS Paths

## Current GCS Usage

The alerting-service persists alert history, routing config snapshots, and cooldown state to cloud storage via `AlertStorageStore` (`alerting_service/persistence/storage_store.py`). All GCS operations use `get_storage_client()` from `unified_cloud_interface` — no direct `google.cloud.storage` imports.

GCS writes happen in two integration points:

- **`core/alert_store.py`** — dual-write: `record_fired()` persists each alert event to GCS history alongside the in-memory store (capped at 1000 events, trimmed to 500 on overflow).
- **`notifiers/router.py`** — every delivery attempt writes an `AlertDeliveryRecord` to GCS history, and routing config is snapshotted to GCS after each routed event.

### Bucket Naming

```
gs://alerting-service-{gcp_project_id}/
```

### Path Templates

| Dataset       | Path Pattern                          | Format | Notes                                             |
| ------------- | ------------------------------------- | ------ | ------------------------------------------------- |
| Alert history | `alerting/history/date={YYYY-MM-DD}/` | JSONL  | One blob per write, UUID-named to avoid conflicts |
| Alert configs | `alerting/configs/`                   | YAML   | Routing rule snapshots for audit                  |
| Alert state   | `alerting/state/cooldowns.json`       | JSON   | Cooldown state for cross-instance coordination    |

### Example Resolved Paths

```
gs://alerting-service-my-project-id/alerting/history/date=2026-03-08/a1b2c3d4e5f6.jsonl
gs://alerting-service-my-project-id/alerting/configs/routing_rules.yaml
gs://alerting-service-my-project-id/alerting/state/cooldowns.json
```

## Secret Manager (Not GCS)

The alerting-service reads credentials from Secret Manager, not GCS:

- `alerting-pagerduty-routing-key` — PagerDuty routing key
- `alerting-slack-webhook-url` — Slack Incoming Webhook URL

Secret Manager paths follow the GCP convention: `projects/{project_id}/secrets/{secret_name}/versions/latest`.
