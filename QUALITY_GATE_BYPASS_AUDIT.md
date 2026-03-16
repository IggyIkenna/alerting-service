# Quality Gate Bypass Audit

## Purpose

This file documents any quality gate bypasses (e.g. reportAny, E501, file size) that are explicitly allowed for this repo. Per strict-quality-gates: all bypasses must be documented here with the specific file, rule, and justification. No bypass may be added without a corresponding entry.

## Current Bypasses

### §1.1 — asyncio.run() in loop (main.py)

**File:** `alerting_service/main.py`
**Rule:** `asyncio.run() in loop — use asyncio.gather()`
**Reason:** False positive. `asyncio.run(main())` is in the `if __name__ == "__main__":` guard — it is the entry point, not inside a loop. The QG heuristic fires because the same file contains a `while` loop inside `_run_subscriber_until_shutdown()`, a separate async function. The `asyncio.run()` call is architecturally correct: it is the outermost event loop entry, not nested inside a loop.

### §2.1 — broad except Exception (persistence + routing)

**Files:** `alerting_service/persistence/storage_store.py`, `alerting_service/notifiers/router.py`, `alerting_service/core/alert_store.py`
**Rule:** `except Exception:` (broad except)
**Reason:** Best-effort persistence handlers. These catch all exceptions to prevent a GCS/storage outage from crashing the alerting pipeline. Each catch site logs the exception via `logger.exception()` — no errors are swallowed. This is the same pattern used in execution-service for resilience-critical handlers (see execution-service QUALITY_GATE_BYPASS_AUDIT.md §5.1).

## Bypass Register

| ID   | File                                            | Rule                    | Type                    | Reviewed | Justification                                                    |
| ---- | ----------------------------------------------- | ----------------------- | ----------------------- | -------- | ---------------------------------------------------------------- |
| §1.1 | `alerting_service/main.py`                      | `asyncio.run() in loop` | False positive          | 2026-03  | Entry point guard pattern; while loop is in a different function |
| §2.1 | `alerting_service/persistence/storage_store.py` | `except Exception:`     | Architectural exception | 2026-03  | Best-effort persistence; all exceptions logged, not swallowed    |
| §2.1 | `alerting_service/notifiers/router.py`          | `except Exception:`     | Architectural exception | 2026-03  | Best-effort persistence; all exceptions logged, not swallowed    |
| §2.1 | `alerting_service/core/alert_store.py`          | `except Exception:`     | Architectural exception | 2026-03  | Best-effort GCS dual-write; exception logged, not swallowed      |

## What Counts as a Bypass

A bypass is any one of:

- A `# type: ignore` comment suppressing a basedpyright error
- A `# noqa` comment suppressing a ruff lint error
- An entry in the QG bypass list that disables a check for this repo
- A `pytest.mark.skip` without a linked issue or expiry date
- Any CI step with `|| true` or `|| :` that masks a real failure

## Adding a New Bypass

To add a new bypass:

1. Add it to this file with file, rule, type (false positive / architectural exception / temporary), review date, and justification.
2. Set an expiry date if temporary (e.g. "expires 2026-06-01 — blocked by upstream fix").
3. Get a second reviewer to sign off before merging to main.

## Audit History

- 2026-03: Initial audit — no bypasses.
- 2026-03: §1.1 added — asyncio.run() false positive in main.py (entry point pattern, not in loop).

## Architectural Notes

**No `# type: ignore` policy**: Per workspace rules (`.cursor/rules/no-type-any-use-specific.mdc`), `# type: ignore` comments that hide architectural violations are prohibited. Fix the root cause or use `cast()` with a comment explaining why it is safe.

**No try/except ImportError policy**: Per `.cursor/rules/no-empty-fallbacks.mdc`, library imports must not be wrapped in `try/except ImportError` fallbacks. Imports must either succeed or fail loudly at startup.

**Notifier failure handling**: The `route_event()` function catches notifier failures by return value (not exception), logs `ALERT_FAILED`, and continues. This is not a bypass — it is deliberate design to prevent a PagerDuty outage from blocking Slack delivery and vice versa.
