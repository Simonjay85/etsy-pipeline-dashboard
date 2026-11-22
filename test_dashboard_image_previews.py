#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import dashboard_app


class TestDashboardImagePreviews(unittest.TestCase):
    def assert_shop_scoped_file_url(
        self,
        url: str,
        *,
        expected_path: str,
        expected_shop: str,
    ) -> None:
        parsed = urlsplit(url)
        self.assertEqual(expected_path, parsed.path)
        query = parse_qs(parsed.query)
        self.assertEqual([expected_shop], query.get("shop_id"))
        self.assertEqual(1, len(query.get("v", [])))
        self.assertTrue(query["v"][0])

    def test_uses_original_when_allocated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "hero image.png"
            image.write_bytes(b"renderable")
            resolved = dashboard_app._renderable_image_url(
                "product-12", image, shop_id="shop-a"
            )
        self.assert_shop_scoped_file_url(
            resolved["url"],
            expected_path="/files/product-12/images/hero%20image.png",
            expected_shop="shop-a",
        )
        self.assertFalse(resolved["preview_only"])
        self.assertFalse(resolved["hydration_needed"])
        self.assertEqual("local", resolved["availability"])

    def test_same_product_filename_has_distinct_cache_key_per_shop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "etsy_01.jpg"
            image.write_bytes(b"same on-disk fixture")
            shop_a = dashboard_app._renderable_image_url(
                "product-166", image, shop_id="daisyflowdigital"
            )
            shop_b = dashboard_app._renderable_image_url(
                "product-166", image, shop_id="shop3"
            )

        self.assertNotEqual(shop_a["url"], shop_b["url"])
        self.assertEqual(
            ["daisyflowdigital"],
            parse_qs(urlsplit(shop_a["url"]).query).get("shop_id"),
        )
        self.assertEqual(
            ["shop3"],
            parse_qs(urlsplit(shop_b["url"]).query).get("shop_id"),
        )

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
            resolved = dashboard_app._renderable_image_url(
                "product-13", image, shop_id="shop-a"
            )
        self.assert_shop_scoped_file_url(
            resolved["url"],
            expected_path=f"/files/product-13/images/.thumbcache/{cache_name}",
            expected_shop="shop-a",
        )
        self.assert_shop_scoped_file_url(
            resolved["full_url"],
            expected_path="/files/product-13/images/preview.png",
            expected_shop="shop-a",
        )
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
            resolved = dashboard_app._renderable_image_url(
                "product-14", image, shop_id="shop-a"
            )
        self.assert_shop_scoped_file_url(
            resolved["url"],
            expected_path="/files/product-14/images/preview.png",
            expected_shop="shop-a",
        )
        self.assertEqual(resolved["url"], resolved["full_url"])
        self.assertTrue(resolved["preview_only"])
        self.assertTrue(resolved["hydration_needed"])
        self.assertEqual("hydration_required", resolved["availability"])

    def test_file_endpoint_rejects_shop_scope_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image = root / "shops" / "shop-a" / "product-01" / "images" / "hero.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"shop-a-image")
            shops = {
                "shop-a": {"id": "shop-a", "name": "Shop A"},
                "shop-b": {"id": "shop-b", "name": "Shop B"},
            }
            with patch.object(dashboard_app, "BASE_DIR", root), \
                    patch.object(dashboard_app, "SHOPS", shops), \
                    patch.object(dashboard_app, "_active_shop_id", "shop-a"):
                response = dashboard_app.serve_file(
                    "product-01", "images", "hero.jpg", shop_id="shop-a", v="fixture"
                )
                with self.assertRaises(dashboard_app.HTTPException) as context:
                    dashboard_app.serve_file(
                        "product-01", "images", "hero.jpg", shop_id="shop-b", v="fixture"
                    )

        self.assertEqual(image, Path(response.path))
        self.assertEqual("private, max-age=31536000, immutable", response.headers["cache-control"])
        self.assertEqual(409, context.exception.status_code)

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

            def fake_resolver(_folder, image_path, *, shop_id=None):
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
