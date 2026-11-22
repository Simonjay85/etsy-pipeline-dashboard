## Project instruction routing

Read the relevant sections of `CLAUDE.md` for dashboard, pipeline, shop-data, posting, synchronization, SEO, or other workflows it defines. For a focused instruction audit or a change that only affects project guidance, read the applicable sections and references without forcing an unrelated full-project read.

## Canonical workspace (only root)

This repository is the **only** canonical Etsy project. Resolve paths from the repo root (`Path(__file__).resolve().parent` for Python scripts living at root) — do not invent parallel hardcoded copies.

| Role | Canonical path |
|------|----------------|
| Project root | `/Users/aaronnguyen/Developer/Etsy` |
| Archive/cloud scope | `/Users/aaronnguyen/Developer/Etsy/master_products` (optional; not a shop intake source) |
| Shop data | `/Users/aaronnguyen/Developer/Etsy/shops` |
| Backups | `/Users/aaronnguyen/Developer/Etsy/output/backup` |

`/Users/aaronnguyen/Documents/Claude/Projects/Etsy` is an **obsolete duplicate**. Never use it as a source or destination for reads, writes, imports, syncs, or backups.

See `shops/AGENTS.md` for shop rules. If an optional `master_products/` archive exists, it is independent archive/cloud data and never a shop import source.

## Agent workflow for project changes

- For every project change, GPT 6.1 Sol Medium (`gpt-6.1-sol`, `reasoning_effort=medium`) performs analysis, planning, and task assignment with explicit scope and acceptance checks.
- GPT 6 Luna Max (`gpt-6-luna`, `reasoning_effort=max`) subagent implements scoped code or project changes and runs proportionate checks.
- After implementation, GPT 6.1 Sol Medium independently reviews the actual diff and verification evidence. Planning and review alone do not count as implementation; delegation does not transfer external mutation authority.
- If a required model or reasoning effort is unavailable in the current runtime, report the exact unavailable role and configuration; do not silently substitute another model or effort. Report any substitution explicitly, and never claim a requested model ran based on configuration alone.
- Continue through the authorized scoped result and run proportionate targeted checks. Report local, staged, deployed, cache-bounded live, and public acceptance states separately when they differ.
- Preserve unrelated staged, unstaged, and untracked work. Do not reset, stage, or rewrite it.
- If a targeted check exposes a failure caused by the requested change, continue the scoped repair until it passes or report the exact blocker.

## Task authorization

- A user request to fix a bug, error, or incorrect behavior authorizes diagnosis, a narrow implementation, and proportionate verification in the real project workspace without repeated approval.
- Existing authorization persists for the current requested task and its in-scope steps. Ask again only for a destructive, high-risk, or new external action that is not already covered by clear task authorization, or for a materially ambiguous product decision.

## Backup policy

- Use `backup_etsy_to_drive.py` for the scheduled daily/weekly Google Drive snapshots; keep the Drive destination scoped to `Etsy Automated Backups` and retain at least 30 versions per cadence.
- Every snapshot must include `manifest.json` with SHA-256 hashes. Never upload a zero-byte or `compressed,dataless` iCloud placeholder; stop and report the exact source that needs hydration.
