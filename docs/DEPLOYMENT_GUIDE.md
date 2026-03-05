# Alerting Service — Deployment Guide

## Overview

Alerting service manages alerts and notifications for the trading system. Deployment is via Cloud Run.

## Prerequisites

- GCP project with Artifact Registry, Cloud Run, Secret Manager
- GCP_PROJECT_ID environment variable set
- Service account with roles: roles/run.invoker, roles/secretmanager.secretAccessor, roles/storage.objectViewer

## Deployment Steps

```bash
gcloud builds submit --tag gcr.io/{project_id}/alerting-service:latest
gcloud run deploy alerting-service \
  --image gcr.io/{project_id}/alerting-service:latest \
  --region {region} \
  --set-env-vars GCP_PROJECT_ID={project_id}
```

## Via deployment-service

Use the deployment UI or API. See deployment-service/configs/ for service definitions.

## Rollback Procedure

Redeploy previous image tag from Artifact Registry.

## Health Check

`GET /health` or equivalent readiness endpoint.
