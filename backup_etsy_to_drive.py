#!/usr/bin/env python3
"""Create versioned Etsy project snapshots and send them to Google Drive.

The Drive remote is intentionally supplied by rclone.  OAuth tokens stay in
rclone's config and are never copied into this repository or the manifest.

Daily snapshots contain the live workbooks, configs, mappings and JSON
snapshots.  Weekly snapshots additionally contain all master products and the
two active shop trees.  Each snapshot has a SHA-256 manifest and retention is
enforced per cadence (30 daily + 30 weekly snapshots by default).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_REMOTE = os.environ.get("ETSY_BACKUP_RCLONE_REMOTE", "gdrive_dest")
DEFAULT_PARENT_ID = os.environ.get(
    "ETSY_BACKUP_DRIVE_PARENT_ID", "1cg5xsQ_3HIPEDASOco9MddHrm993DoCA"
)
DEFAULT_RETENTION = int(os.environ.get("ETSY_BACKUP_RETENTION", "30"))
RCLONE = os.environ.get("ETSY_BACKUP_RCLONE_BIN", "/opt/homebrew/bin/rclone")
LOCK_PATH = Path(os.environ.get("ETSY_BACKUP_LOCK", "/tmp/etsy-backup.lock"))
LOG_PATH = ROOT / "output" / "backup" / "backup.log"


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_dataless(path: Path) -> bool:
    """Return true for an iCloud placeholder whose bytes are not local yet."""
    result = subprocess.run(
        ["/usr/bin/stat", "-f", "%Sf", str(path)], capture_output=True, text=True
    )
    return result.returncode == 0 and "dataless" in result.stdout


def copy_file(source: Path, staging: Path, files: list[dict[str, object]]) -> None:
    if not source.is_file():
        log(f"skip missing file: {source}")
        return
    if is_dataless(source):
        raise RuntimeError(
            f"source is iCloud dataless (hydrate it before backup): {source}"
        )
    relative = Path("files") / source.relative_to(ROOT)
    destination = staging / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    files.append(
        {"path": relative.as_posix(), "size": destination.stat().st_size, "sha256": sha256(destination)}
    )


def copy_tree(source: Path, staging: Path, files: list[dict[str, object]]) -> None:
    if not source.is_dir():
        log(f"skip missing directory: {source}")
        return
    destination = staging / "files" / source.relative_to(ROOT)
    for item in sorted(source.rglob("*")):
        if item.is_symlink() or not item.is_file():
            continue
        if is_dataless(item):
            raise RuntimeError(
                f"source is iCloud dataless (hydrate it before backup): {item}"
            )
        relative = item.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        files.append(
            {
                "path": (Path("files") / source.relative_to(ROOT) / relative).as_posix(),
                "size": target.stat().st_size,
                "sha256": sha256(target),
            }
        )


def collect_sources(kind: str, staging: Path) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []

    # Live catalog workbooks are the authoritative spreadsheet sources.
    copy_file(ROOT / "Etsy_Listing_Template.xlsx", staging, files)
    for shop in ("daisyflowdigital", "templystudios"):
        copy_file(ROOT / "shops" / shop / "Etsy_SEO_Generator.xlsx", staging, files)

    # Config, active-shop context, mapping and generated JSON snapshots.
    for name in ("shops_config.json", "product_source_map.json", "active_shop.txt"):
        copy_file(ROOT / name, staging, files)
    for snapshot in sorted((ROOT / "scratch").rglob("*.json")):
        copy_file(snapshot, staging, files)
    for report in sorted((ROOT / "shops").glob("*/etsy_shop_sync_report_*.json")):
        copy_file(report, staging, files)

    if kind == "weekly":
        copy_tree(ROOT / "master_products", staging, files)
        for shop in ("daisyflowdigital", "templystudios"):
            copy_tree(ROOT / "shops" / shop, staging, files)
    return files


def run_rclone(args: list[str], dry_run: bool = False) -> None:
    command = [RCLONE, *args]
    if dry_run:
        command.append("--dry-run")
    log("run: " + " ".join(command))
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if completed.stdout.strip():
        log(completed.stdout.strip())
    if completed.returncode:
        if completed.stderr.strip():
            log(completed.stderr.strip())
        raise RuntimeError(f"rclone exited with {completed.returncode}")


def retention(remote: str, parent_id: str, kind: str, keep: int, dry_run: bool) -> None:
    base = f"{remote}:{kind}"
    command = [RCLONE, "lsf", "--dirs-only", "--max-depth", "1", base, "--drive-root-folder-id", parent_id]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        log("retention listing skipped: " + (result.stderr.strip() or "rclone error"))
        return
    snapshots = sorted(line.strip().rstrip("/") for line in result.stdout.splitlines() if line.strip())
    for old in snapshots[:-keep]:
        run_rclone(["purge", f"{base}/{old}", "--drive-root-folder-id", parent_id], dry_run=dry_run)


def make_snapshot(kind: str, remote: str, parent_id: str, keep: int, dry_run: bool) -> Path:
    if kind not in {"daily", "weekly"}:
        raise ValueError(kind)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_id = f"{kind}-{stamp}"
    staging_root = Path(tempfile.mkdtemp(prefix=f"etsy-{snapshot_id}-", dir="/tmp"))
    staging = staging_root / snapshot_id
    staging.mkdir()
    try:
        files = collect_sources(kind, staging)
        if not files:
            raise RuntimeError("no backup sources found")
        manifest = {
            "schema": 1,
            "snapshot_id": snapshot_id,
            "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "repository": str(ROOT),
            "file_count": len(files),
            "files": sorted(files, key=lambda item: str(item["path"])),
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if dry_run:
            log(f"dry-run {snapshot_id}: {len(files)} files, manifest={manifest_path}")
        else:
            run_rclone(
                [
                    "copy",
                    str(staging),
                    f"{remote}:{kind}/{snapshot_id}",
                    "--drive-root-folder-id",
                    parent_id,
                    "--checkers",
                    "4",
                    "--transfers",
                    "2",
                ]
            )
            retention(remote, parent_id, kind, keep, dry_run=False)
            log(f"uploaded {snapshot_id}: {len(files)} files")
        return manifest_path
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("daily", "weekly"))
    parser.add_argument("--dry-run", action="store_true", help="build and validate without uploading")
    parser.add_argument("--remote", default=DEFAULT_REMOTE)
    parser.add_argument("--parent-id", default=DEFAULT_PARENT_ID)
    parser.add_argument("--retention", type=int, default=DEFAULT_RETENTION)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another backup is already running; exiting")
            return 0
        try:
            make_snapshot(args.kind, args.remote, args.parent_id, max(1, args.retention), args.dry_run)
        except Exception as exc:  # noqa: BLE001 - log the failure for launchd
            log(f"FAILED: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
