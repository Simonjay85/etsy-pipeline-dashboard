# CLAUDE.md

## Project scope

- Read this file before performing any operation in this project.
- This project is exclusively for Etsy product and shop management.
- Supported workflows include:
  - syncing Etsy listing data and assets to local storage;
  - pushing selected local product changes to existing Etsy listings;
  - creating draft or live Etsy listings through the existing dashboard workflow;
  - creating and managing Etsy product images and digital files;
  - managing shop-local product catalogs;
  - verified cloud backup, restore, and asset offload.
- Do not add eBay, WordPress, Shopify, or unrelated website-management scope to
  this project unless the user explicitly requests a separate integration.

## Canonical checkout and runtime identity

- Do not assume the current directory is the checkout serving the live local
  dashboard.
- Before editing, restarting, or claiming activation of the dashboard:
  1. identify the process listening on port `8090`;
  2. identify that process's working directory;
  3. verify `GET http://127.0.0.1:8090/` returns HTTP 200 and the expected
     `Etsy Pipeline Dashboard` page;
  4. perform code changes in the verified canonical checkout.
- Keep the following claims separate:
  - source file changed;
  - local tests passed;
  - dashboard process reloaded;
  - live dashboard read-back passed;
  - Etsy or cloud state was remotely verified.

## Dashboard API operations

Common operations are defined by the API routes in `dashboard_app.py`, normally
served on port `8090`. Relevant operations may include:

- `POST /api/products/{row}/post`
- `POST /api/products/{row}/push-to-etsy`
- `POST /api/products/{row}/sync-from-etsy`
- `POST /api/run-all-pending`
- `POST /api/stop-all`

The canonical checkout may also expose an operation-queue API. Inspect the
actual active checkout before relying on a route or queue contract.

An accepted request, HTTP 202 response, `queued` status, or `{"ok": true}`
response proves admission only. It does not prove completion. Observe the
authoritative job/queue status and logs, then verify the resulting local,
Etsy, or cloud state before reporting success.

## Shop and product scoping

- Resolve the active shop from `active_shop.txt` before shop-specific work.
- Do not treat `active_shop.txt` as proof of which Etsy account is authenticated
  in Chrome.
- Scope each operation using:
  - shop ID;
  - operation type;
  - product folder or workbook row;
  - Etsy listing ID when applicable.
- Queue and job keys should follow the equivalent of:

  `<shop_id>:<operation>:<product-or-row>:<listing_id-if-any>`

- Before any live Etsy mutation, verify:
  1. active dashboard shop;
  2. job shop ID;
  3. intended workbook and row;
  4. intended product folder;
  5. numeric Etsy listing ID and mapped Etsy URL;
  6. selected fields or requested operation;
  7. current task, process, and queue state;
  8. the exact authenticated Etsy shop in the Chrome/CDP context used by the
     operation.

- Do not change `active_shop.txt` merely to bypass a shop-specific guard.

## Sources of truth

- `active_shop.txt` defines the current dashboard shop context.
- `shops/<shop>/Etsy_SEO_Generator.xlsx` is the operational workbook for that
  shop.
- `shops/<shop>/product-XX/` contains shop-local product assets.
- `product_source_map.json` records source and cross-shop mapping history; it is
  not proof of current Etsy or cloud state.
- Etsy Shop Manager and listing-editor read-back are authoritative for live
  Etsy state.
- Verified remote manifest, file hashes, and current revision pointer are
  authoritative for cloud state.
- Keep each shop's editable workbook and product operations isolated. Do not
  merge Daisy Flow and Temply Studio into a shared editable workbook without
  redesigning every reader, writer, queue key, and product mapping.

## Product-folder and asset rules

- Validate product files before copy, import, upload, sync, or backup:
  - the file exists;
  - the file is materialized locally;
  - size is greater than zero;
  - it is not an iCloud `compressed,dataless` placeholder;
  - the file type and expected dimensions or archive contents are valid.
- Hydrate only the required iCloud files. Do not hydrate the full project tree
  unless necessary.
- Never upload, sync, copy, or back up a zero-byte or dataless placeholder.
- Preserve source/hash manifests so repeated intake is idempotent.
- Do not silently overwrite an existing product folder or mapped workbook row.

## Temply Studio and Daisy Flow boundaries

- Image Factory is Temply Studio-only unless the canonical implementation
  explicitly changes that contract.
- Do not modify `FACTORY_SHOP_ID`, `active_shop.txt`, routes, or shop
  configuration merely to force Daisy Flow through Image Factory.
- Daisy Flow asset intake is shop-local.
- When allocating a new Daisy Flow folder:
  - use the smallest genuinely unused `product-XX`;
  - treat workbook mappings and metadata-only records as used, even if their
    asset folders are empty;
  - create separate `images/` and `files/` directories;
  - deduplicate imported sources using filenames, sizes, and hashes.
- Keep customer-delivery files and listing-gallery images separate.
- Keep product files as separate files unless the user explicitly requests a
  ZIP.

## Task, process, and queue safety

- Use the existing dashboard task/process/queue model.
- Do not start duplicate work for the same scoped shop, operation, and target.
- Serialize operations that share an Etsy Chrome/CDP session or the same
  product/cloud lock.
- `Stop All` must:
  - terminate only tracked subprocesses;
  - cancel and await tracked background tasks;
  - clear pending in-memory queue entries;
  - report which work was killed, cancelled, discarded, or already completed.
- Do not kill unrelated Chrome, Python, Playwright, rclone, or user-owned
  processes.
- Do not delete a lock file merely because an operation timed out. Identify the
  lock holder and active transfer first.
- A dashboard restart discards in-memory queued work. Report discarded commands
  as cancelled or not attempted, never as completed.

## Live Etsy mutation rules

- Use read-only inspection first.
- For posting, publishing, updating, deleting, or syncing Etsy state, verify the
  exact shop, listing, product, requested fields, authenticated session, and
  queue state immediately before mutation.
- Prefer the narrowest operation, such as `images` only, instead of repushing
  unrelated listing fields.
- Do not call a draft a published listing.
- Do not report a listing as updated until the live editor or Shop Manager state
  has been reloaded and verified.
- Preserve partial-failure truth:
  - metadata success does not imply assets success;
  - image success does not imply digital-file success;
  - queued does not imply completed;
  - subprocess exit does not replace Etsy read-back.

## Cloud asset and offload safety

- Cloud upload, cloud verification, local offload, and restore are separate
  operations.
- A prior cloud revision is not proof that the current local files are uploaded.
- Require a verified remote manifest, SHA-256 hashes, file counts, sizes, and
  current-revision pointer before reporting cloud completion.
- Do not delete local assets merely because upload started or an old revision
  exists.
- Local deletion/offload is permitted only by the explicitly requested offload
  workflow after current-source upload and remote verification.
- Report local deletion separately and state whether recovery is possible.

## Secrets and private configuration

- Treat `shops_config.json`, browser profiles, cookies, OAuth tokens, API keys,
  secret files, order/customer information, and shop-private configuration
  defensively.
- Never print, copy into logs, expose in responses, overwrite unintentionally,
  or commit credentials or private shop configuration.
- Do not add secrets to command lines, patches, manifests, test fixtures, or Git.
- Review exact staged files before any commit. Never use a broad staging command
  in a mixed or dirty worktree.

## Backup policy

- Use `backup_etsy_to_drive.py` for scheduled daily and weekly snapshots.
- Keep the Google Drive destination scoped to `Etsy Automated Backups`.
- Retain at least 30 versions for each cadence.
- Every snapshot must contain `manifest.json` with SHA-256 hashes.
- Fail closed when a source is zero-byte, missing, unreadable, symlink-escaping,
  or an iCloud `compressed,dataless` placeholder.
- A local backup archive is not proof of a successful Drive backup. Verify the
  uploaded snapshot and manifest by remote read-back.

## Verification and reporting

Use precise lifecycle language:

- `prepared`: local data or files are ready;
- `queued`: operation was admitted but has not completed;
- `running`: authoritative job or process is active;
- `local_verified`: local files, workbook, or tests were verified;
- `remote_verified`: Etsy or cloud state was read back successfully;
- `partial`: only part of the requested operation succeeded;
- `failed`: attempted and failed;
- `not_attempted`: blocked before execution.

Never collapse these states into a generic “done”. Report the active shop,
target product/listing, requested operation, verification performed, and any
remaining live or remote step.
