from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard_app


class FakeHydratingStore:
    def __init__(self, cache_root: Path):
        self.cache_root = cache_root
        self.calls = []

    def resolve_asset_root(self, product_root: Path) -> dict:
        self.calls.append(Path(product_root))
        return {
            "source": "cloud-cache",
            "asset_root": str(self.cache_root),
            "manifest": {
                "files": [
                    {
                        "path": "images/01-hero.png",
                        "role": "image",
                        "size": 12,
                        "sha256": "unused-test-digest",
                    }
                ]
            },
        }


class TestDashboardCloudPreview(unittest.TestCase):
    def test_descriptor_keeps_preview_small_and_points_full_view_to_verified_image_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            product = Path(temporary) / "product-01"
            product.mkdir()
            (product / dashboard_app.CLOUD_PREVIEW_FILE_NAME).write_bytes(b"preview")
            descriptor = dashboard_app._cloud_preview_descriptor(
                "product-01",
                product,
                {"files": [{"path": "images/01-hero.png", "role": "image"}]},
            )

        self.assertIsNotNone(descriptor)
        self.assertEqual("/api/cloud-assets/preview/product-01", descriptor["url"])
        self.assertEqual(
            "/api/cloud-assets/image/product-01/images/01-hero.png",
            descriptor["full_url"],
        )
        self.assertTrue(descriptor["preview_only"])
        self.assertTrue(descriptor["hydration_needed"])

    def test_preview_endpoint_serves_only_retained_root_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            product = Path(temporary) / "product-01"
            product.mkdir()
            preview = product / dashboard_app.CLOUD_PREVIEW_FILE_NAME
            preview.write_bytes(b"preview")
            with patch.object(dashboard_app, "_cloud_asset_product_root", return_value=product):
                response = asyncio.run(dashboard_app.cloud_assets_preview("product-01"))
        self.assertEqual(preview, Path(response.path))
        self.assertEqual("image/webp", response.media_type)

    def test_full_image_endpoint_hydrates_cache_and_does_not_install_product_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            product = Path(temporary) / "product-01"
            product.mkdir()
            cache = Path(temporary) / "cache" / "rev-1"
            image = cache / "images" / "01-hero.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"cached-image")
            store = FakeHydratingStore(cache)
            with patch.object(dashboard_app, "_cloud_asset_product_root", return_value=product), \
                patch.object(dashboard_app, "get_cloud_asset_store", return_value=store):
                response = asyncio.run(
                    dashboard_app.cloud_assets_image("product-01", "images/01-hero.png")
                )

            self.assertEqual(image, Path(response.path))
            self.assertEqual([product], store.calls)
            self.assertFalse((product / "images" / "01-hero.png").exists())

    def test_full_image_endpoint_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            product = Path(temporary) / "product-01"
            product.mkdir()
            with patch.object(dashboard_app, "_cloud_asset_product_root", return_value=product):
                with self.assertRaises(dashboard_app.HTTPException) as context:
                    asyncio.run(
                        dashboard_app.cloud_assets_image("product-01", "images/../files/source.zip")
                    )
        self.assertEqual(400, context.exception.status_code)


if __name__ == "__main__":
    unittest.main()
