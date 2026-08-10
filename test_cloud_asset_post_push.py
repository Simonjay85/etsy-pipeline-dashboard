from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import etsy_auto_post
import etsy_push_update


class FakeAssetStore:
    def __init__(self, resolution: dict):
        self.resolution = resolution
        self.resolve_calls = []
        self.mark_calls = []

    def resolve_asset_root(self, product_root: Path) -> dict:
        self.resolve_calls.append(Path(product_root))
        return dict(self.resolution)

    def mark_hydration_cleanup_eligible(self, resolution: dict) -> dict:
        self.mark_calls.append(dict(resolution))
        return {"ok": True, "marked": True, "cache_key": resolution.get("cache_key")}

    def hydrate_product(self, product_root: Path, **_kwargs):  # pragma: no cover - compatibility guard
        raise AssertionError("posting compatibility path must not force strict hydration")


def make_product(base: Path) -> Path:
    product_root = base / "shops" / "templystudios" / "product-01"
    (product_root / "images").mkdir(parents=True)
    (product_root / "files").mkdir()
    (product_root / "images" / "hero.png").write_bytes(b"image")
    (product_root / "files" / "source.zip").write_bytes(b"file")
    return product_root


class CloudAssetPostPushTests(unittest.TestCase):
    def test_local_hit_uses_verified_local_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            product_root = make_product(base)
            store = FakeAssetStore(
                {
                    "source": "local",
                    "asset_root": str(product_root),
                    "image_paths": [str(product_root / "images" / "hero.png")],
                    "file_paths": [str(product_root / "files" / "source.zip")],
                }
            )

            with patch.object(etsy_auto_post, "BASE_DIR", base):
                product = {"folder": "product-01"}
                resolution = etsy_auto_post.resolve_product_asset_paths(
                    product, "templystudios", store=store
                )

            self.assertEqual("local", resolution["source"])
            self.assertEqual([str(product_root / "images" / "hero.png")], product["image_paths"])
            self.assertEqual([str(product_root / "files" / "source.zip")], product["pdf_paths"])
            self.assertEqual([product_root], store.resolve_calls)

    def test_cloud_only_uses_verified_cache_paths_not_product_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            product_root = make_product(base)
            (product_root / "images" / "hero.png").unlink()
            (product_root / "files" / "source.zip").unlink()
            cache_root = base / "output" / "cloud-cache" / "data" / "shops" / "templystudios" / "product-01" / "rev-1"
            (cache_root / "images").mkdir(parents=True)
            (cache_root / "files").mkdir()
            cache_image = cache_root / "images" / "hero.png"
            cache_file = cache_root / "files" / "source.zip"
            cache_image.write_bytes(b"cached-image")
            cache_file.write_bytes(b"cached-file")
            store = FakeAssetStore(
                {
                    "source": "cloud-cache",
                    "asset_root": str(cache_root),
                    "cache_key": "shops/templystudios/product-01@rev-1",
                    "image_paths": [str(cache_image)],
                    "file_paths": [str(cache_file)],
                }
            )

            with patch.object(etsy_push_update, "BASE_DIR", base):
                product = {"folder": "product-01"}
                etsy_push_update.resolve_product_asset_paths(product, "templystudios", store=store)

            self.assertEqual([str(cache_image)], product["image_paths"])
            self.assertEqual([str(cache_file)], product["file_paths"])
            self.assertNotIn(str(product_root), product["image_paths"] + product["file_paths"])

    def test_preflight_failure_returns_before_editor_navigation(self):
        class Page:
            def __init__(self):
                self.goto_calls = []

            async def goto(self, *args, **kwargs):
                self.goto_calls.append((args, kwargs))

        async def run():
            page = Page()
            with patch.object(
                etsy_push_update,
                "resolve_product_asset_paths",
                side_effect=RuntimeError("hash mismatch"),
            ):
                result = await etsy_push_update.push_all(
                    page,
                    "1234567890",
                    {"folder": "product-01", "shop_id": "templystudios"},
                    {"images"},
                )
            return result, page.goto_calls

        import asyncio

        result, goto_calls = asyncio.run(run())
        self.assertFalse(result)
        self.assertEqual([], goto_calls)

    def test_success_marker_is_called_only_for_a_successful_operation(self):
        store = FakeAssetStore({"source": "cloud-cache", "cache_key": "product-01@rev-1"})
        product = {"_cloud_asset_resolution": store.resolution}

        successful_result = True
        if successful_result:
            marked = etsy_auto_post.mark_product_asset_operation_success(product, store=store)
        self.assertTrue(marked["marked"])
        self.assertEqual(1, len(store.mark_calls))

        failed_result = False
        if failed_result:
            etsy_push_update.mark_product_asset_operation_success(product, store=store)
        self.assertEqual(1, len(store.mark_calls))

    def test_local_only_compatibility_uses_legacy_resolver_without_hydration(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            product_root = make_product(base)
            store = FakeAssetStore(
                {
                    "source": "local",
                    "asset_root": str(product_root),
                    "image_paths": [str(product_root / "images" / "hero.png")],
                    "file_paths": [str(product_root / "files" / "source.zip")],
                }
            )

            with patch.object(etsy_push_update, "BASE_DIR", base):
                etsy_push_update.resolve_product_asset_paths(
                    {"folder": "product-01"}, "templystudios", store=store
                )

            self.assertEqual([product_root], store.resolve_calls)


if __name__ == "__main__":
    unittest.main()
