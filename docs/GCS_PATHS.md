# Alerting Service — GCS Paths

## Bucket Pattern

`gs://{bucket_name}/alerting/`

## Path Templates

- Alert configs: `{bucket}/alerting/configs/`
- Alert history/state: `{bucket}/alerting/state/`

Variables: `{bucket_name}` from config. See CONFIGURATION.md for bucket configuration.
