# Etsy multi-shop pipeline

The local dashboard is the execution source of truth for the Etsy pipeline. It
keeps a durable SQLite `JobStore` for candidate UUIDs, stage transitions,
leases, quota reservations, receipts and an outbound event queue. ANNA owns the
campaign record, cadence and high-level progress projection. Hermes is an
assigned worker surface; a Hermes card or a worker message never counts as a
completed step without a Dashboard receipt.

The registered shops are `templystudios`, `daisyflowdigital`, `shop3`
(PivotAndPage) and `BubbleByJulie`. Pipeline candidates are digital listings.
The policy is fixed at five verified drafts per Asia/Ho_Chi_Minh day, at most
two per shop, at most ten active candidates, and one ChatGPT UI image job at a
time. The image API adapter is disabled while the budget is 0 USD.

## Local Dashboard API

The Dashboard remains loopback-only on `127.0.0.1:8090`. Mutating requests
require the injected `X-Dashboard-Mutation-Token` header.

- `GET /api/pipeline/health` returns the four shops, policy, quota, Hermes
  gateway/profile hints and ChatGPT CDP/API capability hints.
- `GET /api/pipeline/jobs` and `GET /api/pipeline/jobs/{job_id}` read durable
  candidate progress.
- `POST /api/pipeline/jobs` admits a research or downstream job. Requests are
  idempotent; a research brief without a candidate UUID receives a deterministic
  candidate identity within its campaign.
- `POST /api/pipeline/jobs/{job_id}/claim` and `/heartbeat` use a worker lease
  and fencing token. A stale worker cannot finish a newer attempt.
- `POST /api/pipeline/jobs/{job_id}/manifest` is the product-worker handoff:
  the claimed `create`/`create-product` worker supplies its canonical
  `product_root`, buyer-facing `customer_files`, ten final `image_files`,
  source assets and license provenance. The Dashboard writes and hashes the
  identity-bound manifest only after renewing the lease; invalid or source-only
  outputs are rejected. A create-product handoff may omit images until the
  image worker finishes; a draft adoption must provide exactly ten.
  A claimed `create-verified-draft` job may pass `source_job_id` to atomically
  adopt that accepted manifest for the Etsy poster; the source and draft
  campaign/candidate/shop/product/contract identities must all match. The
  accepted manifest, customer-file and ten-image hashes are persisted in
  JobStore and are compared byte-for-byte with the final draft receipt.
- `POST /api/pipeline/jobs/{job_id}/reserve-quota` holds a draft slot before
  the Etsy side effect. It requires the current poster lease/fence and the
  server-computed Asia/Ho_Chi_Minh day. A held slot survives
  `UNKNOWN`/`RECONCILING`.
- The local poster calls the JobStore `prepare_pipeline_write` command again
  immediately before the browser Save click. That command rechecks the live
  lease and rolls a held reservation into the current policy day before any
  external write; `complete_verified` repeats the check after readback.
- `POST /api/pipeline/jobs/{job_id}/transition` records a stage result. The
  `verified` transition delegates to one atomic completion command. The
  poster receipt must bind job/campaign/shop/product, current fence, numeric
  listing ID, draft readback URL, ten image hashes, customer-file hashes,
  manifest hash and all QA validators; the same SQLite transaction consumes
  the held quota and appends the ANNA outbox event.
- `POST /api/pipeline/jobs/{job_id}/reconcile` is the first step for an
  uncertain Etsy outcome. It only changes `UNKNOWN` to `RECONCILING`; it never
  accepts a receipt or retries the browser action. Claim the job to obtain a
  fresh fence, then submit the readback through `/transition` with
  `status=verified` (or the equivalent `/reconcile/complete` endpoint).
- `GET /api/pipeline/outbox` and `POST /api/pipeline/outbox/{event_id}/ack`
  form an opt-in, token-authenticated pull/ack bridge for ANNA. Pending events
  remain durable while the VPS is offline.

`publish` is retained in the contract enum for history but is not admitted as
an executable v1 stage. The user reviews a verified draft before any future
publication action.

## Assets and poster boundary

Creative Fabrica/source material is stored under `source_assets/` with
provenance and SHA-256 evidence. Buyer deliverables are an explicit
`customer_files` allowlist under `files/`. The poster rejects source assets,
limits the upload set to five files of at most 20 MiB each, and validates final
PDF/SVG/DXF/ZIP artifacts before opening the Etsy editor. SVG/DXF deliveries
are packaged as a new ZIP with a manifest; raw source archives cannot appear in
that ZIP. PDF validation opens and renders every page with the local PyMuPDF
runtime when available.

Each `.pipeline-product.json` manifest is bound to `pipeline_job_id`,
`campaign_id`, `candidate_id`, `product_id` and one of the four registered
`shop_id` values. The poster compares those identities with JobStore before it
claims the Etsy write. For a draft, the poster also loads the manifest's
ordered ten-image allowlist and compares each selected path and current
SHA-256 before claiming quota and again immediately before Save; a resolver or
cache cannot substitute a different gallery. A manifest without that binding
is rejected and cannot fall through to the legacy poster path. Buyer ZIP members are limited to
top-level PDF/SVG/DXF artifacts plus `manifest.json`, readme or license files;
case-insensitive duplicate names and nested/source payloads are rejected.

`Furniture` remains a physical listing type for the PivotAndPage (`shop3`) and
Bubble By Julie shops. A legacy Furniture row stays physical even if an old
PDF/ZIP path is attached. A digital exception must carry explicit
`listing_type=digital` metadata in its pipeline manifest; new pipeline imports
persist that contract explicitly.

## Hermes and ANNA

`hermes_pipeline_adapter.py` builds a shell-free
`hermes kanban create --json` argv with the canonical Etsy workspace, stage
profile, source/customer separation policy, idempotency key and
`publish_allowed=false`. Hermes accepts research, download, product/asset,
image and SEO stages only; it cannot request an Etsy write boundary or the
`create-verified-draft` stage. The historical `reserach` profile is accepted
as an alias for `research`. The adapter does not run on import; a local Mac
worker must call it explicitly.

ANNA registers project `etsy` and workflow
`etsy-verified-draft-campaign` in its implementation catalog. The workflow is
created with a paused schedule and cannot be enabled until local Dashboard/Mac
health and one-product-per-shop acceptance have been verified. A worker can
send a bounded, redacted, idempotent progress projection to
`POST /v1/etsy/pipeline/events`; ANNA appends it to the existing task event
chain and never grants publish or paid-provider capability.

## Verification

From the Etsy checkout:

```sh
./.venv/bin/python3 -m pytest -q --ignore=scripts
node scripts/run_safe_js_tests.mjs
```

From the ANNA implementation worktree:

```sh
./bin/anna test
```

The repository contains focused tests for contracts, artifact packaging,
leases/quota/reconciliation, Dashboard routes, Hermes argv construction, the
ANNA event projection and the pipeline workbench UI. The `scripts/` directory
also contains operator/live scripts; it is intentionally excluded from the
Python unit-test collection because some files are executable workflows rather
than test modules.
