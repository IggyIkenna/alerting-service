# Configuration

## Required Config Fields

| Field | Type | Description |
|-------|------|-------------|
| `gcp_project_id` | str | GCP project ID (inherited from UnifiedCloudConfig) |
| `service_name` | str | Service name for logging (default: `alerting-system`) |

## Environment Variables

See `.env.example` for required env vars:

- `GCP_PROJECT_ID` — GCP project ID
- `GCS_BUCKET_PREFIX` — GCS bucket prefix (default: `alerting-system`)
- `LOG_LEVEL` — Logging level (default: `INFO`)

## ConfigStore

This service uses UnifiedCloudConfig. ConfigStore usage for runtime config persistence is not yet implemented.
