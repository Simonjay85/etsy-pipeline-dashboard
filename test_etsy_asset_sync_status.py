#!/usr/bin/env python3
from __future__ import annotations

import tempfile
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


if __name__ == "__main__":
    unittest.main()
