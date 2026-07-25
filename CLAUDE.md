# CLAUDE.md

## Imported Claude Cowork project instructions

- At the start of each conversation, read this file before performing operations.
- This dashboard project manages Etsy + eBay/WordPress tooling and product pipelines.
- Common tasks are already defined by the existing API routes in:
  - `dashboard_app.py` (Etsy pipeline, default port 8090)
  - `ebay_wp_dashboard/dashboard_app.py` (eBay + WordPress, default port 8091)
- Use the existing queue/process model for task safety:
  - Etsy: `push-to-etsy`, `sync-from-etsy`, `post`, `run-all-pending`
  - Stop-all should terminate tracked subprocesses and clear pending queues.

## Canonical workspace (only root)

This repo is the **only** canonical Etsy project root:

| Role | Canonical path |
|------|----------------|
| Project root | `/Users/aaronnguyen/Developer/Etsy` |
| Factory source | `/Users/aaronnguyen/Developer/Etsy/master_products` |
| Shop data | `/Users/aaronnguyen/Developer/Etsy/shops` |
| Backups | `/Users/aaronnguyen/Developer/Etsy/output/backup` |

- Import Factory reads `master_products`. Asset-intake / image-factory output must land under `master_products` in this repo.
- Resolve paths from the repo root (`Path(__file__).resolve().parent` for root-level scripts). Do not hardcode the obsolete tree.
- `/Users/aaronnguyen/Documents/Claude/Projects/Etsy` is obsolete — never use it as source or destination.

**Before claiming completion:** confirm generated `master_products/product-NN` folders exist in this repo and that the dashboard Import Factory scan can see them.

## Operational notes

- `shops_config.json` / `ebay_wp_config.json` and secret files are JSON configs and should be treated defensively.
- Active context is persisted in:
  - `active_shop.txt` for Etsy
  - `ebay_wp_dashboard/active_site.txt` for eBay/WordPress
- Before changing live behavior, prefer queue-key scoping by active shop/site + folder/row to avoid cross-context clashes.

## Testing & validation

- Run tests: `.venv/bin/python -m pytest` (54 tests, all passing as of 2026-07-23)
- Config: `pytest.ini` at project root; `conftest.py` excludes non-test scripts.

### Test-to-module map (dashboard_app.py and related)

| Test file | Covers |
|-----------|--------|
| `test_dashboard_image_previews.py` | `dashboard_app._renderable_image_url`, image preview/hydration logic |
| `test_etsy_asset_sync_status.py` | `dashboard_app._size_text_to_bytes`, `_etsy_size_matches`, `_extract_asset_sync_status` |
| `test_etsy_draft_delete.py` | `dashboard_app` draft-delete route + `etsy_clean_duplicates` |
| `test_image_factory_import.py` | `dashboard_app` image-factory scan/import endpoints |
| `test_etsy_auto_post.py` | `etsy_auto_post` browser session resolution, draft filter |
| `test_etsy_catalog_ordering.py` | `etsy_catalog.build_unified_catalog`, `load_local_catalog` |
| `test_merge_safe_duplicates.py` | `etsy_catalog.merge_safe_duplicates` digital-hash guard |

### Excluded scripts (not real tests)

`test_seo_fail.py`, `test_tag_pills.py`, `test_tags.py`, `test_translate.py` are manual/interactive scripts excluded via `conftest.py`.
