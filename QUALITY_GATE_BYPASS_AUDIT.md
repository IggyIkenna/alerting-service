# Quality Gate Bypass Audit — alerting-service

**Last updated:** 2026-03-06 (full repo scan)
**Audit methodology:** See `unified-trading-codex/10-audit/QUALITY_GATE_BYPASS_AUDIT.md`
**Goal:** Pass with zero bypasses after full library adoption.

---

## Summary

| Category                         | Count |
| -------------------------------- | ----- |
| File Size Exceptions             | 0     |
| Ruff (`# noqa`) Suppressions    | 0     |
| Basedpyright (`# type: ignore`)  | 0     |
| Broad `except Exception` bypasses | 1    |
| Coverage exceptions              | 0     |

---

## 2.1 File Size Exceptions

None. All Python source files are well within the 900-line limit.

| File                                       | Lines | Status |
| ------------------------------------------ | ----- | ------ |
| `alerting_system/main.py`                  | 32    | OK     |
| `alerting_system/config.py`               | 9     | OK     |
| `.cursor/scripts/check-import-patterns.py` | 267   | OK (tooling, not service code) |

---

## 2.2 Ruff Exceptions (`# noqa`)

None.

---

## 2.3 Basedpyright Exceptions (`# type: ignore`)

None.

---

## 2.4 Broad `except Exception` Bypasses

| File                      | Line | Pattern                   | Category             | Justification                                                                                                              | Action                                                                   |
| ------------------------- | ---- | ------------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `alerting_system/main.py` | 26   | `except Exception as e:`  | TOP_LEVEL_HANDLER    | Top-level async `main()` uses `except Exception` to log `FAILED` event with error details, then re-raises. Does not swallow. | Consider narrowing to specific exceptions when service logic is implemented. |

**Note:** The quality-gates script excludes `main.py` from the broad-except check for this documented bypass (`grep -v "main\.py"`).

---

## 2.5 Coverage

Current coverage: **77.78%** (14/18 lines covered) — meets the 70% minimum threshold.

No exception required.

---

## 2.6 Setup Events Sink (RESOLVED)

`alerting_system/main.py` uses `sink=MockEventSink()` from `unified_events_interface`.
For production batch deployment, replace with `GCSEventSink` from unified-trading-services.

---

## Scan Details

Scan performed against:
- `alerting_system/` (service source)
- `tests/unit/` (unit tests)

### Checks Performed

| Check                                      | Result  | Notes                                     |
| ------------------------------------------ | ------- | ----------------------------------------- |
| `print()` in production code               | PASS    | None found                                |
| `os.getenv()` usage                        | PASS    | None found                                |
| `os.getenv` empty fallback                 | PASS    | None found                                |
| Naive `datetime.now()`                     | PASS    | None found                                |
| Bare `except:`                             | PASS    | None found                                |
| `requests` in async code                   | PASS    | None found                                |
| `asyncio.run()` in loops                   | PASS    | None found                                |
| Imports inside functions                   | PASS    | None found                                |
| `Any` types                                | PASS    | None found                                |
| Raw `response.json()`                      | PASS    | None found                                |
| Empty string fallbacks                     | PASS    | None found                                |
| Empty dict/list fallbacks                  | PASS    | None found                                |
| Hardcoded prod project ID                  | PASS    | None found                                |
| `GCP_PROJECT_ID` env var                   | PASS    | None found                                |
| Domain clients from wrong library          | PASS    | None found                                |
| `setup_events()` without `sink=`           | PASS    | Uses `sink=MockEventSink()` correctly     |
| Credential-file skip in tests              | PASS    | None found                                |
| `GOOGLE_APPLICATION_CREDENTIALS` in .env  | PASS    | No `.env.example` file                    |
| Deep unified lib imports                   | PASS    | None found                                |
| Old event logging import                   | PASS    | Uses `unified_events_interface` directly  |
| Direct cloud SDK imports (STEP 5.5)        | PASS    | None found                                |
| Architecture tier compliance               | PASS    | `REPO_ARCH_TIER=service` (skipped)        |
| Bare `pip install`                         | PASS    | Uses `uv pip install`                     |
| Broad `except Exception`                   | BYPASS  | `main.py` — see §2.4 above                |
| Swallowed errors                           | PASS    | Re-raises after logging                   |
| File size (900 line limit)                 | PASS    | All files within limit                    |
| Function/class/method size limits          | PASS    | All within limits                         |
| `||true` bypass in quality gates           | PASS    | None found                                |
| Unit tests cloud-agnostic                  | PASS    | No real cloud API calls                   |
| Direct cloud SDK outside UCI (STEP 5.10)   | PASS    | None found                                |
| Protocol-leaking symbols (STEP 5.11)       | PASS    | None found                                |
| Hardcoded protocol names (STEP 5.12)       | PASS    | None found                                |
| `test_event_logging.py` present            | PASS    | `tests/unit/test_event_logging.py` exists |
| `test_config.py` present                   | PASS    | `tests/unit/test_config.py` exists        |
| Duplicate test files                       | PASS    | None found                                |
| `@pytest.mark.skip` without reason         | PASS    | None found                                |

---

## References

- Canonical audit methodology: `unified-trading-codex/10-audit/QUALITY_GATE_BYPASS_AUDIT.md`
- Quality gate script: `scripts/quality-gates.sh`
- Coding standards: `unified-trading-codex/06-coding-standards/README.md`
