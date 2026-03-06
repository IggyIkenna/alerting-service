# Quality Gate Bypass Audit

This document records all quality gate bypasses (exceptions) for alerting-service.
Goal: pass with zero bypasses after full library adoption.

## §1.1 — setup_events with sink (RESOLVED)

main.py now uses `sink=MockEventSink()` from unified_events_interface. For production batch, consider GCSEventSink from unified-trading-services.

## §1.2 — broad except Exception (main.py)

**File:** `alerting_system/main.py`
**Check:** broad except Exception — document in QUALITY_GATE_BYPASS_AUDIT.md
**Rationale:** Top-level async main() uses `except Exception` to log FAILED and re-raise; does not swallow.
**Action:** Consider narrowing to specific exceptions when service logic is implemented.
