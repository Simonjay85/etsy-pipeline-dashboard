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
SOURCE_EXCLUDED_NAMES = {
    ".cloud-assets.lock",
}


def is_excluded_source(path: Path) -> bool:
    """Return true for known runtime/diagnostic files that should not be backed up."""
    return path.name in SOURCE_EXCLUDED_NAMES


def manifest_digest(manifest: dict[str, object]) -> str:
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def validate_source_file(path: Path) -> None:
    if path.stat().st_size <= 0:
        raise RuntimeError(f"source is zero-byte (hydrate it before backup): {path}")
    if is_dataless(path):
        raise RuntimeError(f"source is iCloud dataless (hydrate it before backup): {path}")


def copy_file(source: Path, staging: Path, files: list[dict[str, object]], root: Path) -> None:
    if not source.is_file():
        log(f"skip missing file: {source}")
        return
    if is_excluded_source(source):
        log(f"skip excluded file: {source}")
        return
    validate_source_file(source)
    relative = Path("files") / source.relative_to(root)
    destination = staging / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    files.append(
        {"path": relative.as_posix(), "size": destination.stat().st_size, "sha256": sha256(destination)}
    )


def copy_tree(source: Path, staging: Path, files: list[dict[str, object]], root: Path) -> None:
    if not source.is_dir():
        log(f"skip missing directory: {source}")
        return
    destination = staging / "files" / source.relative_to(root)
    for item in sorted(source.rglob("*")):
        if item.is_symlink() or not item.is_file():
            continue
        if is_excluded_source(item):
            log(f"skip excluded file: {item}")
            continue
        validate_source_file(item)
        relative = item.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        files.append(
            {
                "path": (Path("files") / source.relative_to(root) / relative).as_posix(),
                "size": target.stat().st_size,
                "sha256": sha256(target),
            }
        )


def collect_sources(kind: str, staging: Path, files: list[dict[str, object]], root: Path) -> None:
    # Live catalog workbooks are the authoritative spreadsheet sources.
    copy_file(root / "Etsy_Listing_Template.xlsx", staging, files, root)
    for shop in ("daisyflowdigital", "templystudios"):
        copy_file(root / "shops" / shop / "Etsy_SEO_Generator.xlsx", staging, files, root)

    # Config, active-shop context, mapping and generated JSON snapshots.
    for name in ("shops_config.json", "product_source_map.json", "active_shop.txt"):
        copy_file(root / name, staging, files, root)
    for snapshot in sorted((root / "scratch").rglob("*.json")):
        copy_file(snapshot, staging, files, root)
    for report in sorted((root / "shops").glob("*/etsy_shop_sync_report_*.json")):
        copy_file(report, staging, files, root)

    if kind == "weekly":
        copy_tree(root / "master_products", staging, files, root)
        for shop in ("daisyflowdigital", "templystudios"):
            copy_tree(root / "shops" / shop, staging, files, root)


def make_snapshot_manifest(
    kind: str,
    staging: Path,
    root: Path,
    now: datetime,
) -> tuple[Path, dict[str, object]]:
    if kind not in {"daily", "weekly"}:
        raise ValueError(kind)

    files: list[dict[str, object]] = []
    collect_sources(kind, staging, files, root)
    if not files:
        raise RuntimeError("no backup sources found")

    snapshot_id = f"{kind}-{now.strftime('%Y%m%dT%H%M%SZ')}"
    manifest = {
        "schema": 1,
        "snapshot_id": snapshot_id,
        "kind": kind,
        "created_at": now.isoformat(),
        "repository": str(root),
        "file_count": len(files),
        "files": sorted(files, key=lambda item: str(item["path"])),
    }
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path, manifest


def verify_snapshot(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError("manifest invalid: must be an object")

    files = manifest.get("files")
    if not isinstance(files, list):
        raise RuntimeError("manifest invalid: missing file list")

    staging = manifest_path.parent
    for item in files:
        if not isinstance(item, dict):
            raise RuntimeError("manifest file entry invalid")
        path = Path(str(item.get("path")))
        expected_size = int(item.get("size", -1))
        expected_hash = str(item.get("sha256", ""))

        candidate = staging / path
        if not candidate.is_file():
            raise RuntimeError(f"manifest file missing: {path}")
        if candidate.stat().st_size != expected_size:
            raise RuntimeError(f"manifest size mismatch: {path}")
        if sha256(candidate) != expected_hash:
            raise RuntimeError(f"manifest hash mismatch: {path}")


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


def list_snapshots(remote: str, parent_id: str, kind: str, root: Path) -> list[str]:
    base = f"{remote}:{kind}"
    command = [RCLONE, "lsf", "--dirs-only", "--max-depth", "1", base, "--drive-root-folder-id", parent_id]
    result = subprocess.run(command, cwd=root, text=True, capture_output=True)
    if result.returncode:
        log("retention listing skipped: " + (result.stderr.strip() or "rclone error"))
        return []
    return sorted(line.strip().rstrip("/") for line in result.stdout.splitlines() if line.strip())


def plan_retention(remote: str, parent_id: str, kind: str, keep: int, root: Path) -> list[str]:
    keep_count = max(1, keep)
    snapshots = list_snapshots(remote, parent_id, kind, root)
    return snapshots[:-keep_count]


def retention(remote: str, parent_id: str, kind: str, keep: int, dry_run: bool, root: Path) -> None:
    for old_snapshot in plan_retention(remote, parent_id, kind, keep, root):
        run_rclone(
            ["purge", f"{remote}:{kind}/{old_snapshot}", "--drive-root-folder-id", parent_id],
            dry_run=dry_run,
        )


def make_snapshot(
    kind: str,
    remote: str,
    parent_id: str,
    keep: int,
    dry_run: bool,
    now: datetime | None = None,
    root: Path | None = None,
    preserve_staging: bool = False,
) -> Path:
    if kind not in {"daily", "weekly"}:
        raise ValueError(kind)

    effective_root = root or ROOT
    effective_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    snapshot_id = f"{kind}-{effective_now.strftime('%Y%m%dT%H%M%SZ')}"
    staging_root = Path(tempfile.mkdtemp(prefix=f"etsy-{snapshot_id}-", dir="/tmp"))
    staging = staging_root / snapshot_id
    staging.mkdir()
    try:
        manifest_path, manifest = make_snapshot_manifest(
            kind=kind,
            staging=staging,
            root=effective_root,
            now=effective_now,
        )
        verify_snapshot(manifest_path)
        manifest_sha = manifest_digest(manifest)

        if dry_run:
            retention(remote, parent_id, kind, keep, dry_run=True, root=effective_root)
            log(
                f"dry-run {snapshot_id}: {manifest['file_count']} files, "
                f"manifest={manifest_path}, manifest_sha256={manifest_sha}"
            )
            return manifest_path

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
        retention(remote, parent_id, kind, keep, dry_run=False, root=effective_root)
        log(f"uploaded {snapshot_id}: {manifest['file_count']} files")
        return manifest_path
    finally:
        if not preserve_staging:
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
