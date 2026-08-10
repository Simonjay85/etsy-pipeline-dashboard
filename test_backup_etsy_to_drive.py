from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import backup_etsy_to_drive as backup


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _build_minimal_repo(
    root: Path, include_weekly: bool = False, include_runtime_lock: bool = False
) -> None:
    _write(root / "Etsy_Listing_Template.xlsx", b"listing-template")
    _write(root / "shops" / "daisyflowdigital" / "Etsy_SEO_Generator.xlsx", b"daisy-seo")
    _write(root / "shops" / "templystudios" / "Etsy_SEO_Generator.xlsx", b"temply-seo")
    _write(root / "shops_config.json", b"{}")
    _write(root / "product_source_map.json", b"{}")
    _write(root / "active_shop.txt", b"daisyflowdigital")
    _write(root / "scratch" / "snapshot.json", b"{}")
    _write(root / "shops" / "daisyflowdigital" / "etsy_shop_sync_report_alpha.json", b"{}")
    _write(root / "shops" / "templystudios" / "etsy_shop_sync_report_alpha.json", b"{}")

    if include_weekly:
        _write(root / "master_products" / "product-01" / "README.md", b"weekly-product")
        if include_runtime_lock:
            (root / "master_products" / "product-01" / ".cloud-assets.lock").write_bytes(b"")


class BackupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timestamp = datetime(2026, 8, 8, 3, 15, 0, tzinfo=timezone.utc)

    def test_manifest_is_complete_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            _build_minimal_repo(repo)

            with patch.object(backup, "list_snapshots", return_value=[]):
                manifest_path_one = backup.make_snapshot(
                    kind="daily",
                    remote="gdrive_dest",
                    parent_id="parent-id",
                    keep=30,
                    dry_run=True,
                    now=self.timestamp,
                    root=repo,
                    preserve_staging=True,
                )
                manifest_one = json.loads(manifest_path_one.read_text(encoding="utf-8"))
                digest_one = backup.manifest_digest(manifest_one)

                manifest_path_two = backup.make_snapshot(
                    kind="daily",
                    remote="gdrive_dest",
                    parent_id="parent-id",
                    keep=30,
                    dry_run=True,
                    now=self.timestamp,
                    root=repo,
                    preserve_staging=True,
                )
                manifest_two = json.loads(manifest_path_two.read_text(encoding="utf-8"))
                digest_two = backup.manifest_digest(manifest_two)

            try:
                self.assertEqual(manifest_one["kind"], "daily")
                self.assertEqual(manifest_one["repository"], str(repo))
                self.assertGreaterEqual(manifest_one["file_count"], 6)
                self.assertEqual(manifest_one["files"], sorted(manifest_one["files"], key=lambda item: item["path"]))
                self.assertEqual(digest_one, digest_two)
                self.assertEqual(len(manifest_one["files"]), len({entry["path"] for entry in manifest_one["files"]}))
                for entry in manifest_one["files"]:
                    self.assertGreater(entry["size"], 0)
                    self.assertEqual(len(entry["sha256"]), 64)
                    self.assertTrue(entry["path"].startswith("files/"))
            finally:
                shutil.rmtree(manifest_path_one.parent, ignore_errors=True)
                shutil.rmtree(manifest_path_two.parent, ignore_errors=True)

    def test_zero_byte_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            _build_minimal_repo(repo)
            (repo / "Etsy_Listing_Template.xlsx").write_bytes(b"")

            with self.assertRaises(RuntimeError) as caught:
                backup.make_snapshot(
                    kind="daily",
                    remote="gdrive_dest",
                    parent_id="parent-id",
                    keep=30,
                    dry_run=True,
                    now=self.timestamp,
                    root=repo,
                    preserve_staging=True,
                )
            self.assertIn("zero-byte", str(caught.exception))

    def test_dataless_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            _build_minimal_repo(repo)
            blocked = repo / "shops" / "daisyflowdigital" / "Etsy_SEO_Generator.xlsx"

            with patch("backup_etsy_to_drive.is_dataless", side_effect=lambda path: path == blocked):
                with self.assertRaises(RuntimeError) as caught:
                    backup.make_snapshot(
                        kind="daily",
                        remote="gdrive_dest",
                        parent_id="parent-id",
                        keep=30,
                        dry_run=True,
                        now=self.timestamp,
                        root=repo,
                        preserve_staging=True,
                    )
            self.assertIn("dataless", str(caught.exception))

    def test_retention_floor_uses_minimum_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            _build_minimal_repo(repo)

            observed: list[tuple[list[str], bool]] = []

            def fake_run_rclone(args: list[str], dry_run: bool = False) -> None:
                observed.append((args, dry_run))

            with patch.object(backup, "list_snapshots", return_value=["daily-01", "daily-02", "daily-03"]), patch.object(
                backup, "run_rclone", side_effect=fake_run_rclone
            ):
                backup.make_snapshot(
                    kind="daily",
                    remote="gdrive_dest",
                    parent_id="parent-id",
                    keep=0,
                    dry_run=False,
                    now=self.timestamp,
                    root=repo,
                    preserve_staging=True,
                )

            purge_calls = [call for call in observed if call[0][0] == "purge"]
            self.assertEqual(len(purge_calls), 2)
            self.assertTrue(all(call[1] is False for call in purge_calls))
            self.assertIn("gdrive_dest:daily/daily-01", purge_calls[0][0])
            self.assertIn("gdrive_dest:daily/daily-02", purge_calls[1][0])

    def test_destination_scoping_for_copy_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            _build_minimal_repo(repo)

            commands: list[list[str]] = []

            def fake_run_rclone(args: list[str], dry_run: bool = False) -> None:
                commands.append(list(args))

            with patch.object(backup, "run_rclone", side_effect=fake_run_rclone), patch.object(
                backup, "list_snapshots", return_value=[]
            ):
                backup.make_snapshot(
                    kind="daily",
                    remote="gdrive_dest",
                    parent_id="parent-id",
                    keep=30,
                    dry_run=False,
                    now=self.timestamp,
                    root=repo,
                    preserve_staging=True,
                )

            copy_calls = [command for command in commands if command and command[0] == "copy"]
            self.assertEqual(len(copy_calls), 1)
            copy_target = copy_calls[0][2]
            self.assertTrue(copy_target.startswith("gdrive_dest:daily/"))
            self.assertNotIn("/Users/aaronnguyen/Documents/Claude/Projects/Etsy", " ".join(copy_calls[0]))
            for command in commands:
                self.assertNotIn("/Users/aaronnguyen/Documents/Claude/Projects/Etsy", " ".join(command))

    def test_weekly_snapshot_skips_cloud_assets_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            _build_minimal_repo(repo, include_weekly=True, include_runtime_lock=True)

            with patch.object(backup, "list_snapshots", return_value=[]):
                manifest_path = backup.make_snapshot(
                    kind="weekly",
                    remote="gdrive_dest",
                    parent_id="parent-id",
                    keep=30,
                    dry_run=True,
                    now=self.timestamp,
                    root=repo,
                    preserve_staging=True,
                )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            paths = [entry["path"] for entry in manifest["files"]]
            self.assertNotIn("files/master_products/product-01/.cloud-assets.lock", paths)
            self.assertTrue(any(path.endswith("master_products/product-01/README.md") for path in paths))

    def test_source_plists_and_script_do_not_use_documents_path(self) -> None:
        root = Path(__file__).resolve().parent
        script_text = (root / "backup_etsy_to_drive.py").read_text(encoding="utf-8")
        self.assertNotIn("/Users/aaronnguyen/Documents/Claude/Projects/Etsy", script_text)

        for plist_name in ("com.user.etsy-backup.daily.plist", "com.user.etsy-backup.weekly.plist"):
            plist_path = root / plist_name
            plist_text = plist_path.read_text(encoding="utf-8")
            self.assertNotIn("/Users/aaronnguyen/Documents/Claude/Projects/Etsy", plist_text)
            self.assertIn("/Users/aaronnguyen/Developer/Etsy", plist_text)
