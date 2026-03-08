# Quality Gate Bypass Audit

## Purpose

This file documents any quality gate bypasses (e.g. reportAny, E501, file size) that are explicitly allowed for this repo. Per strict-quality-gates: all bypasses must be documented here.

## Current Bypasses

### §1.1 — asyncio.run() in loop (main.py)

**File:** `alerting_service/main.py`
**Rule:** `asyncio.run() in loop — use asyncio.gather()`
**Reason:** False positive. `asyncio.run(main())` is in the `if __name__ == "__main__":` guard — it is the entry point, not inside a loop. The QG heuristic fires because the same file contains a `while` loop inside `_run_subscriber_until_shutdown()`, a separate async function. The `asyncio.run()` call is architecturally correct: it is the outermost event loop entry, not nested inside a loop.

## Audit History

- 2026-03: Initial audit — no bypasses.
- 2026-03: §1.1 added — asyncio.run() false positive in main.py (entry point pattern, not in loop).
