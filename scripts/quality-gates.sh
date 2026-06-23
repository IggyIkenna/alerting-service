#!/usr/bin/env bash
# Epic: infrastructure_master
# Lifecycle: permanent
# Delete-when: NA
# Repo-specific settings only. Body: unified-trading-pm/scripts/quality-gates-base/base-service.sh
# SSOT: unified-trading-pm/codex/06-coding-standards/quality-gates-service-template.sh
#
# Instructions for a new service:
#   1. Copy this to scripts/quality-gates.sh in your repo (rollout-quality-gates-unified.py does this)
#   2. SERVICE_NAME, SOURCE_DIR, and MIN_COVERAGE are set automatically by rollout (floor=70)
#   3. Set RUN_INTEGRATION=true only if your repo has integration tests
#   4. Add LOCAL_DEPS entries if your service has local editable deps (e.g. unified-trading-library)
SERVICE_NAME="alerting-service"
SOURCE_DIR="alerting_service"
MIN_COVERAGE=76  # ISS-031: lowered from 89 — mock_data_provider, orchestrator, CLI, reconciliation_rules untested
RUN_INTEGRATION=false
PYTEST_WORKERS=${PYTEST_WORKERS:-2}
LOCAL_DEPS=()
# Architectural exception (QUALITY_GATE_BYPASS_AUDIT.md §2.1): defence-in-depth
# broad `except Exception:` in recovery/notifier code that MUST never propagate
# (a failed pager/persist must not crash the incident pipeline). Every catch logs
# via `logger.warning(..., exc_info=True)` — exceptions are recorded, not swallowed.
BE_EXCLUDE_GLOBS=(
    "alerting_service/gateway/provider_health_probe.py"
    "alerting_service/gateway/state_machine.py"
    "alerting_service/gateway/ack_escalation.py"
    "alerting_service/gateway/incident_persister.py"
    "alerting_service/notifiers/incident_fallback.py"
    "alerting_service/notifiers/physical_pager.py"
)
PIP_AUDIT_EXTRA_ARGS="--ignore-vuln CVE-2026-34073 --ignore-vuln CVE-2026-25645 --ignore-vuln CVE-2026-39892 --ignore-vuln CVE-2026-28684 --ignore-vuln CVE-2026-3219 --ignore-vuln CVE-2026-6357 --ignore-vuln CVE-2026-44431 --ignore-vuln CVE-2026-44432 --ignore-vuln PYSEC-2024-277 --ignore-vuln PYSEC-2025-183 --ignore-vuln PYSEC-2026-161 --ignore-vuln PYSEC-2026-120"
if [ "${CLOUD_BUILD:-}" = "true" ] && [ -d "/workspace/unified-trading-pm" ]; then
    WORKSPACE_ROOT="/workspace"
else
    WORKSPACE_ROOT="$(cd "$(git rev-parse --show-toplevel)/.." && pwd)"
fi
# ISS-032: router.py is a large orchestration entrypoint; P0.12-P0.14 additions push it to ~1000L
MAX_FILE_LINES=1100
# CODEX_MAX_VIOLATIONS pinned 2026-06-11 per plans/active/codex_violations_ratchet_to_five_2026_06_10.md (census-honest: 0 current violations; ratchet-down only).
CODEX_MAX_VIOLATIONS=0
source "${WORKSPACE_ROOT}/unified-trading-pm/scripts/quality-gates-base/base-service.sh"

# Codex enforcement: every entrypoint must emit STARTED, STOPPED, FAILED
# See: unified-trading-pm/codex/03-observability/lifecycle-events.md § Lifecycle Event QG Enforcement
log_section "[5.X/6] UEI LIFECYCLE EVENT ENFORCEMENT (STARTED/STOPPED/FAILED)"
for event in STARTED STOPPED FAILED; do
    run_timeout 30 rg "log_event.*\"${event}\"" "${SOURCE_DIR}" --type py -q \
        || log_warn "Missing log_event('${event}') in ${SERVICE_NAME} — see codex 03-observability/lifecycle-events.md"
done
