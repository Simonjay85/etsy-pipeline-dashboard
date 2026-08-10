# Google Drive cloud asset store for Etsy

The canonical checkout contains the Phase 1 immutable store and the Phase 2
hydration seams. Local metadata/workbooks/code remain authoritative while full
`images/` and `files/` content can be archived remotely and consumed from a
verified cache when a posting/update operation needs it.

## Contract

The store accepts only these local product roots:

```text
shops/<shop>/<product>/
  images/
  files/
master_products/<product>/
  images/
  files/
```

Both `images/` and `files/` must contain at least one regular, non-zero-byte,
hydrated file. Symlinks, path traversal, macOS `compressed,dataless`
placeholders, and incomplete products fail closed. A root-level
`.cloud-preview.webp` is optional and is included in the revision manifest when
present. Upload attempts to generate it from the first deterministic image
using Pillow or the local `cwebp` utility; conversion is best-effort and never
weakens asset or remote verification. Finder `.DS_Store` entries are ignored.

Each upload creates an immutable revision below the existing rclone Drive
remote and parent folder:

```text
assets/v1/
  shops/<shop>/<product>/revisions/<revision>/
    images/...
    files/...
    .cloud-preview.webp       # optional
    manifest.json              # canonical JSON
  current.json                 # pointer written after verification only
```

`manifest.json` contains a SHA-256 and byte count for every asset plus
aggregate image/file/preview/total counts and byte totals. The local product
state is `.cloud-assets.json`; the per-product lock is `.cloud-assets.lock`.
OAuth tokens are not part of this contract. rclone continues to own its
credential storage.

## CLI

Run from the canonical checkout:

```bash
python3 cloud_asset_cli.py upload --path shops/templystudios/product-01
python3 cloud_asset_cli.py verify --path shops/templystudios/product-01
python3 cloud_asset_cli.py restore --path shops/templystudios/product-01
python3 cloud_asset_cli.py status --path shops/templystudios/product-01 --check-remote
python3 cloud_asset_cli.py inventory
python3 cloud_asset_cli.py maintain --dry-run
```

The default routing is `gdrive_dest` with the existing Etsy Automated Backups
Drive parent ID. A secret-free example is provided at
`cloud_asset_store.config.example.json`; a working local override may be saved
as `cloud_asset_store.config.json` or supplied with `--config`. Environment
overrides are limited to routing, cache, and policy settings. The CLI never
prints rclone configuration contents.

The state machine is:

```text
LOCAL_ONLY -> UPLOADING -> CLOUD_VERIFIED
                                  |
                    RESTORING -> RESTORE_VERIFIED -> READY_LOCAL
                                  |
                    OFFLOAD_SCHEDULED -> CLOUD_ONLY

CLOUD_VERIFIED/READY_LOCAL -> DIRTY_LOCAL -> UPLOADING
Any failed operation -> ERROR
```

`verify` is a no-install restore verification: it downloads the immutable
revision to a temporary directory, validates the manifest and every content
hash, and records the configured eligibility deadline. When the local bytes
also match, it moves the product to `OFFLOAD_SCHEDULED`; missing local content
remains `CLOUD_ONLY`, and mismatched local content remains `DIRTY_LOCAL`.

`maintain` is dry-run by default. A real offload requires all of the following
at the same time: `--apply`, an enabled offload policy, an exact product-key
allowlist entry, a previously successful temporary restore verification whose
stored `eligible_after` has passed under the configured `offload.age_days`,
current local bytes whose hashes still equal the manifest, successful remote
re-verification, and a final temporary restore verification immediately before
deletion. A just-in-time restore verification cannot establish the prior soak
window. Only the contents of `images/` and `files/` are removed; state, the
remote revision, `current.json`, and the optional preview remain. Dry-run may
write an audit receipt but does not change the local lifecycle state or
eligibility timestamps. Every attempted operation writes an audit receipt under
`output/cloud-cache/audit/`.

The hydration cache primitives use `output/cloud-cache/data/` and
`output/cloud-cache/hydration-metadata/`. Success and failure entries have
separate TTL metadata; failed-operation metadata is retained for the approved
seven-day retry window by default. Successful browser/post consumers mark the
cache entry eligible and it expires after 24 hours. Cache cleanup never removes
the source product folder.

## Phase 2A hydration API

Browser/post scripts should call the public API:

```python
resolution = store.hydrate_product(product_root, purpose="browser")
page.locator("input[type=file]").set_input_files(resolution["images"])
```

The method resolves the canonical product identity, reads and validates the
remote `current.json` pointer and immutable `manifest.json`, and then returns
either:

- `mode="local"` when the product's `images/` and `files/` bytes exactly match
  the current manifest; or
- `mode="cloud"` when local content is absent or `CLOUD_ONLY`. The immutable
  revision is downloaded and verified under `output/cloud-cache/data/` (or the
  configured cache root) and is never installed into the product folder.

The result includes explicit `images` and `files` path lists, `product_root`,
`revision`, `manifest_hash`/`manifest_sha256`, the validated pointer and
manifest, cache-hit metadata, and a `cleanup` object. The older
`image_paths`/`file_paths` aliases remain available for existing posting code.
Cache success TTL and seven-day failure TTL come from `CloudAssetConfig` and
are injectable in deterministic `LocalRemote` tests. Call
`mark_hydration_cleanup_eligible(resolution)` only after the browser/post
operation succeeds, then run `cleanup_cache()` or
`cleanup_hydration_cache()`; an expired success cache entry is removed only
after that marker exists. Cleanup never removes source product assets.

## Phase 2 dashboard and posting integration

The dashboard exposes read/status and explicitly triggered mutation routes:

```text
GET  /api/cloud-assets/status
POST /api/cloud-assets/upload
POST /api/cloud-assets/verify
POST /api/cloud-assets/restore
POST /api/cloud-assets/cancel-offload
POST /api/cloud-assets/maintain
```

Every mutation must carry `shop_id`, `scope` (`shop` or `master`) and a direct
`product-N` folder. The backend checks the active shop and canonical path
containment before calling the store. Maintenance remains dry-run unless its
request includes the explicit apply/policy/exact-allowlist gates.

The dashboard UI keeps a folder-level cloud status map, filters Local,
Scheduled, Cloud-only and Error products, and provides explicit Upload & verify,
Restore and Cancel offload actions. Cloud status failures do not block the
normal local product list. A retained `.cloud-preview.webp` is served without
hydrating the full gallery; opening a full image uses a manifest-verified
read-only cache endpoint.

`etsy_auto_post.py` and `etsy_push_update.py` resolve verified asset paths before
opening an Etsy editor. Local-only products continue through the compatibility
resolver. Cloud-only products hydrate to `output/cloud-cache/data/...`; a hash,
path, or remote failure stops before Etsy navigation. Successful post/push
operations mark cache cleanup eligibility; failed operations retain it for
retry. No cloud operation performs an Etsy write by itself.

`etsy_shop_sync.py` exposes an injectable safe candidate marker for a completed
local asset sync, but does not mark every workbook reconciliation as dirty when
it has not written asset bytes. The current factory importer does not guess a
master-product identity from its shop output; master hydration must wait for an
explicit master mapping.

## Read-only pilot gate

Run the deterministic catalog gate before any Drive write. The pilot has two
independent modes:

* `shop-only` validates and writes only `shops/<shop>/<product>`.
* `shop-and-master` validates and writes both the shop product and its
  explicitly mapped `master_products/<product>` package.

The default remains `shop-and-master` for backward compatibility. Use
`--mode shop-only` when a shop asset pilot must not depend on the separate
master/factory catalog.

```bash
python3 cloud_asset_pilot.py plan \
  --repo-root /Users/aaronnguyen/Developer/Etsy \
  --shop templystudios \
  --mode shop-only \
  --snapshot scratch/etsy_manager_current_YYYYMMDD_HHMMSS.json
```

Both modes require a fresh same-shop Shop Manager snapshot, numeric listing
IDs in the workbook, unique direct product folders, and complete local shop
assets. Only `shop-and-master` additionally requires an explicit
`master_product`/`master_products`/`master` mapping and complete master assets.
The planner sorts `shop-only` candidates by shop bytes and numeric folder; it
sorts `shop-and-master` candidates by combined shop/master bytes and numeric
folder. Fewer than five eligible rows returns `BLOCKED_CATALOG_MAPPING` with
an empty selection; it never substitutes an ambiguous listing and never
writes state, workbook, Drive, or local assets.

If a future operator run receives five eligible products, the separate execution
path requires both `--execute` and `--confirm-cloud-write`. Its result is
`CLOUD_PIPELINE_VERIFIED` after upload/verify for the selected mode's scopes,
never `LIVE_ETSY_UPLOAD_VERIFIED`, and it still never deletes local assets.

## Scheduled maintenance

The backup launchd plists now point to `/Users/aaronnguyen/Developer/Etsy` and
`com.user.etsy-cloud-maintenance.daily.plist` runs the maintenance CLI in its
safe default dry-run mode. Installing or bootstrapping launchd remains a
separate, explicit operator action; this implementation does not bootstrap it.
