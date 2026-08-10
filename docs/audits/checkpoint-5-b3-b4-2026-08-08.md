# Checkpoint 5 — B3/B4 catalog and asset safety

Date: 2026-08-08

Implemented in the canonical checkout:

- Canonical active-shop `save_to_excel` writes route through `catalog_repository.apply_catalog_update` with per-shop locking, same-filesystem temp validation, atomic replacement, recovery rollback, and optional expected hash/version preconditions.
- PATCH precondition mismatch returns HTTP 409 and never persists `_expected_*` fields.
- Image uploads stage every incoming file, sanitize/deduplicate names, run `AssetReadinessEngine` for the complete set, and only replace final files after the set passes.
- Image-field Etsy updates fail closed on missing/zero-byte/dataless/corrupt/checksum-blocked local images; metadata-only updates do not invoke this gate.

Focused evidence:

- `test_catalog_repository.py` + `test_asset_readiness.py` + integration tests: 24 passed.
- Combined catalog/asset/job/stop focused run: 9 passed in the final D1/D2-compatible run.
- `py_compile` and `git diff --check`: passed.

No production workbook, Etsy, or Drive mutation was performed. The B4 gate is locally implemented only until the canonical process is safely restarted and read back.
