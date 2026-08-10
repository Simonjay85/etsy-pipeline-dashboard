# Phase E — controlled rollout and read-back

Date: 2026-08-08

## Local implementation

- Canonical root: `/Users/aaronnguyen/Developer/Etsy`.
- Branch/HEAD: `main` / `354e080`; no staging, commit, or push.
- Full local Python suite: 402 passed, 39 subtests passed.
- Full local JS suite: passed.
- `git diff --check`, targeted `py_compile`, and Node syntax checks: passed.

## Live owner proof

- PID 17396 owns TCP `*:8090`.
- `lsof` reports cwd `/Users/aaronnguyen/Developer/Etsy`.
- `GET /api/services` returns HTTP 200 from that process.
- Active shop readback remains `templystudios` from the local/dashboard evidence.
- `GET /api/runtime-health` and `GET /api/etsy/jobs` return 404, proving the live process has not loaded this rollout.

## External and scheduler gates

- User-owned monitor PID 30056 remains active and its log reached `182/226 product-458`; it was not stopped or restarted.
- Backup plists lint successfully and point at the canonical Developer checkout, but `launchctl list` has no `com.user.etsy-backup.daily` or `com.user.etsy-backup.weekly` label.
- Weekly backup dry-run fails closed on the real zero-byte payload:
  `shops/daisyflowdigital/.deleted_local_products/product-110_deleted_20260806_163921_1786009161022772000/files/_file_cleanup_backup/Mother's Day SVG Bundle vol 4.zip.001`.
- Scheduler installation, live restart, Etsy mutation, Drive upload, and source hydration remain blocked until the exact source is owner-approved and hydrated.

## Rollback

No live deployment was performed. Local rollback is the existing allowlisted diff/file-level revert plan after owner review; no broad reset/clean/checkout was used. The active process remains untouched.
