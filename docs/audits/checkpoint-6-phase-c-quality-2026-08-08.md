# Checkpoint 6 — Phase C quality gates

Date: 2026-08-08

- `pytest.ini` now excludes `node_modules` from discovery and registers `unit`, `integration`, `browser`, and `external_contract` markers.
- Gallery channel tests now use the strict guard's explicit template mode for the shipped example; obsolete source-page/coverage assertions were aligned with the current contract boundary without weakening the guard.
- Manager-link UI expectations were aligned with the current fail-closed `unavailable` behavior.
- Test isolation for the durable JobStore and single-sync busy set is deterministic.

Final evidence:

- Full Python suite: **402 passed, 39 subtests passed**.
- Full repository JS suite (`test_*.js`): all passed.
- Remaining output from aggregate UI tests is expected diagnostic logging for the intentional unavailable-catalog branch; no assertion failed.
- Two dependency deprecation warnings remain (`google.genai.types` and Starlette/httpx).
