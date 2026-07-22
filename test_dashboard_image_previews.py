#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard_app


class TestDashboardImagePreviews(unittest.TestCase):
    def test_uses_original_when_allocated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "hero image.png"
            image.write_bytes(b"renderable")
            resolved = dashboard_app._renderable_image_url("product-12", image)
        self.assertEqual("/files/product-12/images/hero%20image.png", resolved["url"])
        self.assertFalse(resolved["preview_only"])
        self.assertFalse(resolved["hydration_needed"])
        self.assertEqual("local", resolved["availability"])

    def test_sparse_original_uses_hydrated_md5_thumbnail_without_reading_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_dir = Path(tmpdir) / "images"
            cache_dir = image_dir / ".thumbcache"
            cache_dir.mkdir(parents=True)
            image = image_dir / "preview.png"
            with image.open("wb") as handle:
                handle.truncate(8 * 1024 * 1024)
            if image.stat().st_blocks != 0:
                self.skipTest("filesystem does not expose sparse files with st_blocks=0")
            cache_name = f"{hashlib.md5(image.name.encode()).hexdigest()[:12]}_180.webp"
            (cache_dir / cache_name).write_bytes(b"hydrated preview")
            resolved = dashboard_app._renderable_image_url("product-13", image)
        self.assertEqual(f"/files/product-13/images/.thumbcache/{cache_name}", resolved["url"])
        self.assertEqual("/files/product-13/images/preview.png", resolved["full_url"])
        self.assertTrue(resolved["preview_only"])
        self.assertTrue(resolved["hydration_needed"])
        self.assertEqual("cached_preview", resolved["availability"])

    def test_sparse_original_without_cache_falls_back_to_original_preview_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "images" / "preview.png"
            image.parent.mkdir()
            with image.open("wb") as handle:
                handle.truncate(4 * 1024)
            if image.stat().st_blocks != 0:
                self.skipTest("filesystem does not expose sparse files with st_blocks=0")
            resolved = dashboard_app._renderable_image_url("product-14", image)
        self.assertEqual("/files/product-14/images/preview.png", resolved["url"])
        self.assertEqual("/files/product-14/images/preview.png", resolved["full_url"])
        self.assertTrue(resolved["preview_only"])
        self.assertTrue(resolved["hydration_needed"])
        self.assertEqual("hydration_required", resolved["availability"])

    def test_image_api_preserves_original_size_and_returns_preview_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            shop = Path(tmpdir)
            image_dir = shop / "product-12" / "images"
            cache_dir = image_dir / ".thumbcache"
            cache_dir.mkdir(parents=True)
            image = image_dir / "one.png"
            with image.open("wb") as handle:
                handle.truncate(4 * 1024 * 1024)
            if image.stat().st_blocks != 0:
                self.skipTest("filesystem does not expose sparse files with st_blocks=0")
            cache_name = f"{hashlib.md5(image.name.encode()).hexdigest()[:12]}_180.webp"
            (cache_dir / cache_name).write_bytes(b"preview")
            with patch.object(dashboard_app, "SHOP_DIR", return_value=shop), patch.object(
                dashboard_app, "get_product_by_row", return_value={"folder": "product-12"}
            ):
                result = asyncio.run(dashboard_app.list_images(4))
        self.assertEqual(4 * 1024 * 1024, result["images"][0]["size"])
        self.assertTrue(result["images"][0]["preview_only"])
        self.assertTrue(result["images"][0]["hydration_needed"])

    def test_products_from_excel_uses_cached_or_local_thumb_and_keeps_hydration_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            shop = Path(tmpdir)
            excel_path = shop / "Etsy_SEO_Generator.xlsx"
            wb = dashboard_app.openpyxl.Workbook()
            ws = wb.active
            ws.title = "Listings"
            ws["B4"], ws["H4"] = "product-01", "Title"
            ws["E4"] = 4.99
            wb.save(excel_path)

            folder = shop / "product-01" / "images"
            folder.mkdir(parents=True)
            (folder / "local.png").write_text("local")
            (folder / "cloud.png").write_text("cloud")

            def fake_resolver(_folder, image_path):
                if image_path.name == "cached.png":
                    return {
                        "url": "/files/product-01/images/.thumbcache/cached.webp",
                        "full_url": "/files/product-01/images/cached.png",
                        "preview_only": True,
                        "hydration_needed": True,
                        "availability": "cached_preview",
                    }
                if image_path.name == "local.png":
                    return {
                        "url": "/files/product-01/images/local.png",
                        "full_url": "/files/product-01/images/local.png",
                        "preview_only": False,
                        "hydration_needed": False,
                        "availability": "local",
                    }
                if image_path.name == "cloud.png":
                    return {
                        "url": "/files/product-01/images/cloud.png",
                        "full_url": "/files/product-01/images/cloud.png",
                        "preview_only": True,
                        "hydration_needed": True,
                        "availability": "hydration_required",
                    }
                return None

            (folder / "cached.png").write_text("cached")

            with patch.object(dashboard_app, "EXCEL_FILE", return_value=excel_path), \
                patch.object(dashboard_app, "SHOP_DIR", return_value=shop), \
                patch.object(dashboard_app, "_renderable_image_url", side_effect=fake_resolver):
                product = dashboard_app.products_from_excel()[0]

        self.assertEqual("/files/product-01/images/.thumbcache/cached.webp", product["thumb"])
        self.assertEqual(
            ["/files/product-01/images/.thumbcache/cached.webp", "/files/product-01/images/local.png"],
            product["all_images"],
        )
        self.assertEqual(
            [
                "/files/product-01/images/.thumbcache/cached.webp",
                "/files/product-01/images/cloud.png",
                "/files/product-01/images/local.png",
            ],
            [img["url"] for img in product["image_previews"]],
        )
        self.assertEqual("cached_preview", product["image_previews"][0]["availability"])
        self.assertTrue(product["image_previews"][1]["hydration_needed"])


if __name__ == "__main__":
    unittest.main()
