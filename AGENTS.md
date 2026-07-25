## Imported Claude Cowork project instructions

At the start of every conversation, immediately read the file /Users/aaronnguyen/Developer/Etsy/CLAUDE.md before doing anything else. This file contains the full workflow, folder structure, and instructions for this Etsy project. After reading it, you will understand the setup completely and won't need to ask clarifying questions for common tasks like "đăng sản phẩm lên Etsy", "generate SEO", "tình trạng sản phẩm", etc.

## Canonical workspace (only root)

This repository is the **only** canonical Etsy project. Resolve paths from the repo root (`Path(__file__).resolve().parent` for Python scripts living at root) — do not invent parallel hardcoded copies.

| Role | Canonical path |
|------|----------------|
| Project root | `/Users/aaronnguyen/Developer/Etsy` |
| Factory source | `/Users/aaronnguyen/Developer/Etsy/master_products` |
| Shop data | `/Users/aaronnguyen/Developer/Etsy/shops` |
| Backups | `/Users/aaronnguyen/Developer/Etsy/output/backup` |

`/Users/aaronnguyen/Documents/Claude/Projects/Etsy` is an **obsolete duplicate**. Never use it as a source or destination for reads, writes, imports, syncs, or backups.

Also see `master_products/AGENTS.md` and `shops/AGENTS.md` for factory vs shop rules.

## Model workflow for project changes

- When modifying this project, use Sol Medium for analysis, planning, and technical review.
- Use a GPT-5.3 Codex Spark sub-agent to implement the approved code changes.
- Only when GPT-5.3 Codex Spark cannot run the task or is not exposed by the current runtime, delegate the implementation to a GPT Luna High sub-agent.
- If neither GPT-5.3 Codex Spark nor GPT Luna High is available, use the strongest available coding sub-agent and explicitly report the substitution; do not stop at analysis.
- After implementation, use Sol Medium to review the changes and verification results.
- Do not treat analysis or review as implementation; the code change must be executed and verified in the real project workspace.

## Bug reports imply authorization to fix

- When the user reports a bug, error, or incorrect behavior, treat the request as authorization to diagnose the root cause, implement the narrow fix, and verify it in the real project workspace without asking again.
- Ask for confirmation only when the fix would be genuinely destructive or high-risk, or when a materially ambiguous product decision cannot be resolved safely from the project context.

## Backup policy

- Use `backup_etsy_to_drive.py` for the scheduled daily/weekly Google Drive snapshots; keep the Drive destination scoped to `Etsy Automated Backups` and retain at least 30 versions per cadence.
- Every snapshot must include `manifest.json` with SHA-256 hashes. Never upload a zero-byte or `compressed,dataless` iCloud placeholder; stop and report the exact source that needs hydration.
