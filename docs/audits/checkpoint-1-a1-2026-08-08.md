# Checkpoint 1 — Phase A1 backup scheduler and integrity

## Result

**Implementation PASS; operational weekly dry-run BLOCKED by an existing zero-byte payload.** No Drive upload or shop mutation occurred.

## Changed files

- `backup_etsy_to_drive.py`
  - Added deterministic manifest digesting.
  - Added zero-byte and dataless source validation with exact source errors.
  - Added staging manifest creation, size/hash read-back verification, and explicit retention planning.
  - Added testable repository-root injection and staging preservation.
  - Excludes only the known zero-byte runtime lock name `.cloud-assets.lock`; payload validation remains strict.
- `test_backup_etsy_to_drive.py`
  - Added seven focused tests for manifest determinism/completeness, zero-byte and dataless rejection, retention floor, destination scope, canonical plist paths, and runtime-lock exclusion.
- Existing daily/weekly plists were reviewed and passed syntax validation; their content was not changed in this checkpoint.

## Verification

- `./.venv/bin/python -m py_compile backup_etsy_to_drive.py test_backup_etsy_to_drive.py`: PASS.
- `./.venv/bin/python -m pytest -q test_backup_etsy_to_drive.py`: **7 passed**.
- `git diff --check`: PASS.
- `plutil -lint com.user.etsy-backup.daily.plist`: PASS.
- `plutil -lint com.user.etsy-backup.weekly.plist`: PASS.
- Daily dry-run: PASS; 84 nonzero payload files, manifest written to temporary staging, manifest SHA-256 logged. Retention read-only listing reported the remote cadence directory as not found; no write was attempted.
- Weekly dry-run: correctly stopped before upload on the exact source below:
  `/Users/aaronnguyen/Developer/Etsy/shops/daisyflowdigital/.deleted_local_products/product-110_deleted_20260806_163921_1786009161022772000/files/_file_cleanup_backup/Mother's Day SVG Bundle vol 4.zip.001`

## Runtime mutations

- None from this checkpoint.
- LaunchAgents were not installed/loaded because the weekly source-integrity gate is not safe to claim while a real zero-byte payload exists.
- The existing bulk sync process remains user-owned and active; no restart/stop was attempted.

## Preserved dirty work

All pre-existing modified and untracked files remain intact. No reset, clean, checkout, delete, stage, commit, or mirror write was used.

## Rollback

Rollback is limited to the reviewed A1 hunks in `backup_etsy_to_drive.py` and the new focused test file. Do not revert unrelated dirty work. No runtime rollback is needed because no LaunchAgent was installed and no cloud write occurred.

## Unresolved risk / owner action

The weekly scheduler cannot be operationally certified until the exact zero-byte source is hydrated/repaired or an owner-approved backup-scope decision is made for that recovery artifact. Excluding it silently would violate the backup safety contract. This is not an authorization to edit or delete the source.

## Next checkpoint

Continue with the independent A2 local security boundary work. Revisit A1 after the source is owner-approved and re-run both real-root dry-runs before loading the two LaunchAgents.
