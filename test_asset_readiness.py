#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from asset_readiness import AssetReadinessError, classify_assets, sha256_for_file


VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


class AssetReadinessEngineTests(unittest.TestCase):
    def test_classifies_ready_asset_when_it_has_expected_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "ready.png"
            _write(asset, VALID_PNG)
            expected_sha = sha256_for_file(asset)

            report = classify_assets(
                [str(asset)],
                expected_sha256_by_path={str(asset): expected_sha},
                raise_on_blocked=False,
            )

            self.assertTrue(report.is_ready)
            self.assertEqual(len(report.items), 1)
            item = report.items[0]
            self.assertEqual(item.status, "ready")
            self.assertEqual(item.reason, "asset is ready")
            self.assertEqual(item.remediation, "no action required")
            self.assertEqual(item.expected_sha256, expected_sha)
            self.assertEqual(item.actual_sha256, expected_sha)

    def test_classifies_missing_asset_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.png"

            report = classify_assets([str(missing)], raise_on_blocked=False)

            self.assertFalse(report.is_ready)
            self.assertEqual(report.blocked_items[0].status, "missing")
            self.assertEqual(report.blocked_items[0].reason, "asset file does not exist")
            self.assertEqual(
                report.blocked_items[0].remediation,
                "restore/download the file into the selected product folder",
            )

    def test_classifies_zero_byte_asset_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            zero_byte = Path(temporary) / "zero-byte.jpg"
            _write(zero_byte, b"")

            report = classify_assets([zero_byte], raise_on_blocked=False)

            self.assertFalse(report.is_ready)
            self.assertEqual(report.blocked_items[0].status, "zero-byte")
            self.assertEqual(report.blocked_items[0].reason, "asset is zero-byte")
            self.assertEqual(report.blocked_items[0].remediation, "re-export or restore binary content for this file")

    def test_classifies_dataless_asset_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataless = Path(temporary) / "dataless.png"
            _write(dataless, VALID_PNG)

            with patch("asset_readiness.is_dataless", side_effect=lambda path, info=None: str(path) == str(dataless)):
                report = classify_assets([dataless], raise_on_blocked=False)

            self.assertFalse(report.is_ready)
            self.assertEqual(report.blocked_items[0].status, "dataless")
            self.assertEqual(report.blocked_items[0].reason, "asset is an iCloud dataless placeholder")
            self.assertEqual(
                report.blocked_items[0].remediation,
                "hydrate this placeholder in Finder/iCloud before upload",
            )

    def test_classifies_corrupt_non_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            container = Path(temporary) / "corrupt-asset"
            container.mkdir()

            report = classify_assets([container], raise_on_blocked=False)

            self.assertFalse(report.is_ready)
            self.assertEqual(report.blocked_items[0].status, "corrupt")
            self.assertEqual(report.blocked_items[0].reason, "asset path is not a regular file")
            self.assertEqual(report.blocked_items[0].remediation, "replace this asset with a valid file")

    def test_classifies_corrupt_image_decode_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bad_image = Path(temporary) / "bad-image.png"
            _write(bad_image, b"not-a-png-bytes")

            report = classify_assets([bad_image], raise_on_blocked=False)

            self.assertFalse(report.is_ready)
            self.assertEqual(report.blocked_items[0].status, "corrupt")
            self.assertTrue(report.blocked_items[0].reason.startswith("cannot decode image:"))
            self.assertIn("replace this image with a valid asset file", report.blocked_items[0].remediation)

    def test_classifies_cloud_only_by_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / "cloud-only.jpg"
            report = classify_assets(
                [str(selected)],
                cloud_only=[selected],
                raise_on_blocked=False,
            )

            self.assertFalse(report.is_ready)
            self.assertEqual(report.blocked_items[0].status, "cloud-only")
            self.assertEqual(report.blocked_items[0].reason, "asset exists only in cloud cache")
            self.assertEqual(
                report.blocked_items[0].remediation,
                "hydrate or restore this asset from cloud before upload",
            )

    def test_classifies_cloud_only_by_explicit_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = classify_assets(
                [{"path": str(Path(temporary) / "spec-cloud-only.png"), "cloud_only": True}],
                raise_on_blocked=False,
            )

            self.assertFalse(report.is_ready)
            self.assertEqual(report.blocked_items[0].status, "cloud-only")
            self.assertEqual(report.blocked_items[0].reason, "asset is marked as cloud-only explicitly")
            self.assertEqual(
                report.blocked_items[0].remediation,
                "hydrate or restore this asset from cloud before upload",
            )

    def test_classifies_checksum_mismatch_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checksum.txt"
            _write(path, b"checksum source")
            expected = "0" * 64

            report = classify_assets(
                [str(path)],
                expected_sha256_by_path={str(path): expected},
                raise_on_blocked=False,
            )

            self.assertFalse(report.is_ready)
            self.assertEqual(report.blocked_items[0].status, "checksum-mismatch")
            self.assertEqual(report.blocked_items[0].reason, "asset checksum does not match expected SHA-256")
            self.assertEqual(report.blocked_items[0].expected_sha256, expected)
            self.assertEqual(report.blocked_items[0].actual_sha256, hashlib.sha256(b"checksum source").hexdigest())

    def test_mixed_readiness_reports_blocked_items_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready.png"
            bad = Path(temporary) / "bad.png"
            missing = Path(temporary) / "missing.jpg"
            _write(ready, VALID_PNG)
            _write(bad, b"not-a-png-bytes")

            report = classify_assets([ready, bad, {"path": str(missing), "cloud_only": False}], raise_on_blocked=False)

            self.assertFalse(report.is_ready)
            self.assertEqual(len(report.items), 3)
            self.assertEqual(report.items[0].status, "ready")
            self.assertEqual(report.items[1].status, "corrupt")
            self.assertEqual(report.items[2].status, "missing")

            blocked = report.blocked_items
            self.assertEqual(len(blocked), 2)
            self.assertEqual(blocked[0].path, str(bad))
            self.assertEqual(blocked[1].path, str(missing))
            self.assertTrue(blocked[0].reason.startswith("cannot decode image:"))
            self.assertEqual(blocked[1].reason, "asset file does not exist")

    def test_mixed_readiness_blocks_upload_update_with_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready.png"
            _write(ready, VALID_PNG)
            missing = Path(temporary) / "missing.png"
            with self.assertRaises(AssetReadinessError) as caught:
                classify_assets([ready, missing])

            self.assertIn("asset readiness blocked", str(caught.exception))
            self.assertIn(f"{missing}: asset file does not exist", str(caught.exception))

    def test_no_files_deleted_during_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.png"
            second = root / "second.png"
            _write(first, VALID_PNG)
            _write(second, VALID_PNG)
            before = {path.name: path.stat().st_size for path in root.iterdir() if path.is_file()}

            report = classify_assets([first, root / "missing.png"], raise_on_blocked=False)
            after = {path.name: path.stat().st_size for path in root.iterdir() if path.is_file()}

            self.assertEqual(report.items[1].status, "missing")
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
