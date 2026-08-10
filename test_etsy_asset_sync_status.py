#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import dashboard_app


class TestAssetSyncStatus(unittest.IsolatedAsyncioTestCase):
    def test_etsy_displayed_size_requires_a_close_local_match(self) -> None:
        expected = dashboard_app._size_text_to_bytes("4.99 MB")
        self.assertEqual(4_990_000, expected)
        self.assertTrue(dashboard_app._etsy_size_matches(5_000_000, expected))
        self.assertFalse(dashboard_app._etsy_size_matches(3_000_000, expected))
        self.assertFalse(dashboard_app._etsy_size_matches(5_000_000, None))

    def test_partial_images_are_not_complete(self) -> None:
        status = dashboard_app._extract_asset_sync_status({
            "images_found": 10,
            "images_downloaded": 9,
            "files_section_observed": True,
            "files_found": 0,
            "files_downloaded": 0,
        }, metadata_ok=True)
        self.assertFalse(status["images_complete"])
        self.assertFalse(status["assets_complete"])
        self.assertFalse(status["overall"])

    def test_unobserved_file_section_is_not_complete(self) -> None:
        status = dashboard_app._extract_asset_sync_status({
            "images_found": 10,
            "images_downloaded": 10,
            "files_section_observed": False,
            "files_found": 0,
            "files_downloaded": 0,
        }, metadata_ok=True)
        self.assertTrue(status["images_complete"])
        self.assertFalse(status["files_complete"])
        self.assertFalse(status["overall"])

    async def test_asset_sync_does_not_overwrite_lifecycle_status(self) -> None:
        details = {
            "ok": True,
            "title": "Title",
            "description": "Description",
            "tags": "one, two",
            "price": 4.99,
            "_asset_sync": {
                "images_found": 1,
                "images_downloaded": 1,
                "files_section_observed": True,
                "files_found": 0,
                "files_downloaded": 0,
            },
        }
        writes = []
        with tempfile.TemporaryDirectory() as tmpdir, \
            patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
                patch.object(dashboard_app, "scrape_listing_details", new=AsyncMock(return_value=details)), \
                patch.object(dashboard_app, "save_to_excel", side_effect=lambda row, updates, excel_path=None: writes.append(dict(updates))):
            await dashboard_app._sync_local_from_etsy(
                listing_id="123",
                row=4,
                shop_id="templystudios",
                product_path=Path(tmpdir) / "product-1",
                excel_path=Path(tmpdir) / "book.xlsx",
            )
        self.assertTrue(writes)
        self.assertFalse(any("status" in update for update in writes))

    async def test_sync_local_from_etsy_metadata_only_does_not_download_assets(self) -> None:
        writes = []
        details_payload = {
            "ok": True,
            "title": "Title",
            "description": "Description",
            "tags": "one, two",
            "price": 4.99,
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
            patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
            patch.object(dashboard_app, "scrape_listing_details", new=AsyncMock(return_value=details_payload)) as scrape_mock, \
            patch.object(dashboard_app, "save_to_excel", side_effect=lambda row, updates, excel_path=None: writes.append(dict(updates))):

            details, asset_sync, sync_status, sync_ok, synced_fields = await dashboard_app._sync_local_from_etsy(
                listing_id="123",
                row=4,
                shop_id="templystudios",
                product_path=Path(tmpdir) / "product-1",
                excel_path=Path(tmpdir) / "book.xlsx",
                sync_assets=False,
            )

        scrape_mock.assert_awaited_once()
        called_args = scrape_mock.await_args.args
        called_kwargs = scrape_mock.await_args.kwargs
        self.assertEqual("123", called_args[0])
        self.assertEqual("templystudios", called_kwargs.get("shop_id"))
        self.assertIsNone(called_kwargs.get("product_path"))
        self.assertTrue(sync_ok)
        self.assertTrue(sync_status["metadata_ok"])
        self.assertFalse(sync_status["assets_complete"])
        self.assertTrue(sync_status["assets_deferred"])
        self.assertTrue(sync_status["overall"])
        self.assertEqual({}, asset_sync)
        self.assertIn("title", details)
        self.assertIn("description", details)
        self.assertIn("tags", details)
        self.assertIn("price", details)
        self.assertEqual(
            {"title": "Title", "description": "Description", "tags": "one, two", "price": 4.99},
            details,
        )
        self.assertIn({"title": "Title", "description": "Description", "tags": "one, two", "price": 4.99}, writes)


class TestStagedDownloadSelection(unittest.IsolatedAsyncioTestCase):
    async def test_stable_staged_match_waits_for_expected_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_dir = Path(tmpdir)
            expected_size = 8_192
            staged_file = staging_dir / "etsy-download.pdf.part"
            staged_file.write_bytes(b"")
            baseline = dashboard_app._snapshot_staged_files(staging_dir)

            async def grow_file():
                await asyncio.sleep(0.05)
                staged_file.write_bytes(b"x" * 2048)
                await asyncio.sleep(0.08)
                staged_file.write_bytes(b"y" * expected_size)

            writer_task = asyncio.create_task(grow_file())
            matches = await dashboard_app._await_stable_matching_staged_download(
                staging_dir,
                baseline=baseline,
                expected_size=expected_size,
                max_attempts=30,
                poll_ms=20,
                stable_rounds=2,
                min_size=100,
            )
            await writer_task
            self.assertEqual([staged_file], matches)

    async def test_ambiguous_staged_matches_are_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_dir = Path(tmpdir)
            expected_size = 2048
            baseline = dashboard_app._snapshot_staged_files(staging_dir)
            (staging_dir / "candidate-a.part").write_bytes(b"z" * expected_size)
            (staging_dir / "candidate-b.part").write_bytes(b"z" * expected_size)

            matches = await dashboard_app._await_stable_matching_staged_download(
                staging_dir,
                baseline=baseline,
                expected_size=expected_size,
                max_attempts=5,
                poll_ms=20,
                stable_rounds=2,
                min_size=100,
            )
            self.assertEqual(
                sorted([staging_dir / "candidate-a.part", staging_dir / "candidate-b.part"]),
                sorted(matches),
            )

    async def test_missing_size_match_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_dir = Path(tmpdir)
            baseline = dashboard_app._snapshot_staged_files(staging_dir)
            match = await dashboard_app._await_stable_matching_staged_download(
                staging_dir,
                baseline=baseline,
                expected_size=None,
            )
            self.assertEqual([], match)


if __name__ == "__main__":
    unittest.main()
