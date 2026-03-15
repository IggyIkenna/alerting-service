#!/usr/bin/env bash
# Repo-specific settings only. Body: unified-trading-pm/scripts/quality-gates-base/base-service.sh
# SSOT: unified-trading-codex/06-coding-standards/quality-gates-service-template.sh
#
# Instructions for a new service:
#   1. Copy this to scripts/quality-gates.sh in your repo (rollout-quality-gates-unified.py does this)
#   2. SERVICE_NAME, SOURCE_DIR, and MIN_COVERAGE are set automatically by rollout (floor=70)
#   3. Set RUN_INTEGRATION=true only if your repo has integration tests
#   4. Add LOCAL_DEPS entries if your service has local editable deps (e.g. unified-events-interface)
SERVICE_NAME="alerting-service"
SOURCE_DIR="alerting_service"
MIN_COVERAGE=89
RUN_INTEGRATION=false
PYTEST_WORKERS=${PYTEST_WORKERS:-2}
LOCAL_DEPS=()
# config-bootstrap: main.py reads LOG_LEVEL before UnifiedCloudConfig is available
OS_ENV_EXCLUDE_GLOBS=("--glob" "!**/main.py")
# Lazy imports to avoid circular dependency (router.py -> storage_store.py)
IMPORT_INSIDE_EXCLUDE_GLOBS=("!**/notifiers/router.py")
# Mock-mode paths use inline imports for conditional loading
IMPORT_INSIDE_EXCLUDE_GLOBS+=("!**/api/routes/alerts.py" "!**/api/routes/delivery_status.py")
# Broad except in persistence layer — logs exception detail, returns safe default
BE_EXCLUDE_GLOBS=("**/persistence/storage_store.py" "**/notifiers/router.py")
WORKSPACE_ROOT="$(cd "$(git rev-parse --show-toplevel)/.." && pwd)"
source "${WORKSPACE_ROOT}/unified-trading-pm/scripts/quality-gates-base/base-service.sh"

# Codex enforcement: every entrypoint must emit STARTED, STOPPED, FAILED
# See: unified-trading-codex/03-observability/lifecycle-events.md § Lifecycle Event QG Enforcement
log_section "[5.X/6] UEI LIFECYCLE EVENT ENFORCEMENT (STARTED/STOPPED/FAILED)"
for event in STARTED STOPPED FAILED; do
    run_timeout 30 rg "log_event.*\"${event}\"" "${SOURCE_DIR}" --type py -q \
        || log_warn "Missing log_event('${event}') in ${SERVICE_NAME} — see codex 03-observability/lifecycle-events.md"
done
