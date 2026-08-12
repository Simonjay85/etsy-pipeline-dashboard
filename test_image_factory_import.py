import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl
from starlette.requests import Request

import dashboard_app
import sync_factory_to_shop as sync_factory


def _make_workbook(path: Path, rows=()):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Listings"
    for row_num, folder, keyword in rows:
        ws.cell(row=row_num, column=2, value=folder)
        ws.cell(row=row_num, column=3, value=keyword)
    wb.save(path)


def _request(payload: dict) -> Request:
    body = json.dumps(payload).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/api/image-factory/import", "headers": []}, receive)


def _json_from_response(resp):
    if isinstance(resp, dict):
        return resp
    return json.loads(resp.body.decode("utf-8"))


def _active_shop_env(temp_dir: Path, shop_id: str = "templystudios"):
    shop_dir = temp_dir / "shops" / shop_id
    shop_dir.mkdir(parents=True, exist_ok=True)
    excel = shop_dir / "Etsy_SEO_Generator.xlsx"
    return shop_dir, excel


class ImageFactoryDashboardTests(unittest.TestCase):
    def test_scan_strict_layout_excludes_legacy_and_non_product_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "shops" / "templystudios"
            shop, excel = _active_shop_env(base, "templystudios")
            _make_workbook(excel, [
                (4, "product-01", "sample product"),
                (5, "legacy-row", "ignored"),
            ])

            source.mkdir(parents=True, exist_ok=True)
            # Valid source
            valid = source / "valid-slug-77"
            (valid / "etsy-images-chatgpt-final").mkdir(parents=True)
            (valid / "source-zip").mkdir(parents=True)
            (valid / "etsy-images-chatgpt-final" / "01_hero_image.png").write_bytes(b"x")
            (valid / "source-zip" / "My Product.zip").write_bytes(b"zip")

            # Legacy layout (invalid) - missing source-zip
            legacy = source / "legacy-source"
            (legacy / "images").mkdir(parents=True)
            (legacy / "images" / "01_hero_image.png").write_bytes(b"x")

            # Direct product-* source should be rejected
            product_slug = source / "product-01"
            (product_slug / "etsy-images-chatgpt-final").mkdir(parents=True)
            (product_slug / "source-zip").mkdir(parents=True)
            (product_slug / "etsy-images-chatgpt-final" / "01_hero_image.png").write_bytes(b"x")
            (product_slug / "source-zip" / "My Product.zip").write_bytes(b"zip")

            # Excluded names
            excluded = source / "_asset_quarantine"
            (excluded / "etsy-images-chatgpt-final").mkdir(parents=True)
            (excluded / "source-zip").mkdir(parents=True)
            (excluded / "etsy-images-chatgpt-final" / "01_hero_image.png").write_bytes(b"x")
            (excluded / "source-zip" / "My Product.zip").write_bytes(b"zip")

            # Imported destination folder for duplicate mapping
            imported_product = shop / "product-01" / "files"
            imported_product.mkdir(parents=True)
            (imported_product / "My Product.zip").write_bytes(b"old")

            folders = dashboard_app._scan_factory_folders(source, shop, excel)
            self.assertEqual([item["name"] for item in folders], ["valid-slug-77"])
            self.assertEqual(folders[0]["already_imported"], True)
            self.assertEqual(folders[0]["imported_folder"], "product-01")

    def test_scan_requires_usables_only_and_requires_both_dirs(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "shops" / "templystudios"
            shop, excel = _active_shop_env(base, "templystudios")
            _make_workbook(excel, [(4, "product-01", "sample")])
            source.mkdir(parents=True, exist_ok=True)

            source_only_image = source / "just-image"
            (source_only_image / "etsy-images-chatgpt-final").mkdir(parents=True)
            (source_only_image / "etsy-images-chatgpt-final" / "01_hero_image.png").write_bytes(b"x")

            source_only_zip = source / "just-zip"
            (source_only_zip / "source-zip").mkdir(parents=True)
            (source_only_zip / "source-zip" / "My Product.zip").write_bytes(b"x")

            source_empty = source / "bad-zip"
            (source_empty / "etsy-images-chatgpt-final").mkdir(parents=True)
            (source_empty / "source-zip").mkdir(parents=True)
            (source_empty / "etsy-images-chatgpt-final" / ".DS_Store").write_bytes(b"x")
            (source_empty / "source-zip" / "My Product.zip.bak").write_bytes(b"x")

            folders = dashboard_app._scan_factory_folders(source, shop, excel)
            self.assertEqual(folders, [])

            valid = source / "ready"
            (valid / "etsy-images-chatgpt-final").mkdir(parents=True)
            (valid / "source-zip").mkdir(parents=True)
            (valid / "etsy-images-chatgpt-final" / "thumb.webp").write_bytes(b"x")
            (valid / "source-zip" / "ready.zip").write_bytes(b"zip")
            (valid / "source-zip" / "ready.zip.bak").write_bytes(b"zip")
            self.assertEqual(len(dashboard_app._scan_factory_folders(source, shop, excel)), 1)

    def test_import_requires_shop_id_and_active_shop(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source_root = base / "shops" / "templystudios" / "ready"
            (source_root / "etsy-images-chatgpt-final").mkdir(parents=True)
            (source_root / "source-zip").mkdir(parents=True)
            (source_root / "etsy-images-chatgpt-final" / "cover.png").write_bytes(b"x")
            (source_root / "source-zip" / "ready.zip").write_bytes(b"zip")

            source_root.parent.mkdir(parents=True, exist_ok=True)
            shop, excel = _active_shop_env(base, "templystudios")
            _make_workbook(excel)

            with (
                patch.object(dashboard_app, "BASE_DIR", base),
                patch.object(dashboard_app, "IMAGE_FACTORY_OUTPUT", base / "shops" / "templystudios"),
            ):
                # Missing shop_id
                bad_payload = _request({"folders": ["ready"], "auto_seo": False})
                missing = asyncio.run(dashboard_app.import_from_factory(bad_payload))
                self.assertEqual(_json_from_response(missing)["ok"], False)

                # Active shop mismatch
                with (
                    patch.object(dashboard_app, "_active_shop_id", "daisyflowdigital"),
                    patch.object(dashboard_app, "get_active_shop", return_value={"name": "Daisy"}),
                ):
                    mismatch = asyncio.run(dashboard_app.import_from_factory(_request({
                        "folders": ["ready"],
                        "shop_id": "templystudios",
                        "auto_seo": False,
                    })))
                    self.assertEqual(getattr(mismatch, "status_code", None), 409)

                # Correct binding
                with (
                    patch.object(dashboard_app, "_active_shop_id", "templystudios"),
                    patch.object(dashboard_app, "get_active_shop", return_value={"name": "Temply"}),
                    patch.object(dashboard_app, "copy_image_with_watermark", lambda src, dst, _watermark: shutil.copy2(src, dst)),
                ):
                    ok = _json_from_response(asyncio.run(dashboard_app.import_from_factory(_request({
                        "folders": ["ready"],
                        "shop_id": "templystudios",
                        "auto_seo": False,
                    }))))
                    self.assertTrue(ok["ok"])
                    self.assertEqual(ok["imported"], 1)

    def test_source_folder_resolver_rejects_traversal_and_invalid_names(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source_root = base / "shops" / "templystudios"
            source_root.mkdir(parents=True)
            good = source_root / "ready"
            (good / "etsy-images-chatgpt-final").mkdir(parents=True)
            (good / "source-zip").mkdir(parents=True)
            (good / "etsy-images-chatgpt-final" / "cover.png").write_bytes(b"x")
            (good / "source-zip" / "ready.zip").write_bytes(b"zip")

            with (
                patch.object(dashboard_app, "IMAGE_FACTORY_OUTPUT", source_root),
            ):
                self.assertEqual(dashboard_app._resolve_factory_source_folder("ready"), good)
                self.assertIsNone(dashboard_app._resolve_factory_source_folder("../ready"))
                self.assertIsNone(dashboard_app._resolve_factory_source_folder("product-01"))
                self.assertIsNone(dashboard_app._resolve_factory_source_folder("missing"))


class SyncFactoryScriptTests(unittest.TestCase):
    def _prepare_sync_env(self, temp: Path):
        base = temp
        source = base / "shops" / "templystudios"
        source.mkdir(parents=True)
        shop = base / "shops" / "templystudios"
        excel = shop / "Etsy_SEO_Generator.xlsx"
        (shop).mkdir(parents=True, exist_ok=True)
        _make_workbook(excel, [])
        return source, shop, excel

    def test_sync_factory_filters_exact_layout_and_usable_files(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, _, excel = self._prepare_sync_env(base)

            valid = source / "ready"
            (valid / "etsy-images-chatgpt-final").mkdir(parents=True)
            (valid / "source-zip").mkdir(parents=True)
            (valid / "etsy-images-chatgpt-final" / "a.png").write_bytes(b"x")
            (valid / "etsy-images-chatgpt-final" / "zero.jpg").write_bytes(b"")
            (valid / "source-zip" / "a-product.zip").write_bytes(b"zip")
            (valid / "source-zip" / "temp.zip.bak").write_bytes(b"zip")

            invalid = source / "legacy"
            (invalid / "images").mkdir(parents=True)
            (invalid / "images" / "a.png").write_bytes(b"x")
            (invalid / "files").mkdir(parents=True)
            (invalid / "files" / "a.pdf").write_bytes(b"x")

            with (
                patch.object(sync_factory, "BASE_DIR", base),
                patch.object(sync_factory, "SRC_DIR", source),
                patch.object(sync_factory, "assert_temple_shop_active", return_value=True),
                patch.object(sync_factory, "load_shop_config", return_value={}),
            ):
                self.assertTrue(sync_factory._is_supported_factory_source_dir(valid))
                self.assertFalse(sync_factory._is_supported_factory_source_dir(invalid))
                self.assertEqual([f.name for f in sync_factory.get_factory_images(valid)], ["a.png"])
                self.assertEqual([f.name for f in sync_factory.get_factory_files(valid)], ["a-product.zip"])
                self.assertEqual(sync_factory.get_factory_files(invalid), [])

    def test_sync_shop_refuses_already_imported_and_active_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, shop, excel = self._prepare_sync_env(base)

            (shop / "product-01").mkdir()
            (shop / "product-01" / "files").mkdir()
            (shop / "product-01" / "files" / "repeat.zip").write_bytes(b"old")

            _make_workbook(excel, [(4, "product-01", "repeat")])
            src = source / "ready"
            (src / "etsy-images-chatgpt-final").mkdir(parents=True)
            (src / "source-zip").mkdir(parents=True)
            (src / "etsy-images-chatgpt-final" / "cover.png").write_bytes(b"x")
            (src / "source-zip" / "repeat.zip").write_bytes(b"new")

            with (
                patch.object(sync_factory, "BASE_DIR", base),
                patch.object(sync_factory, "SRC_DIR", source),
                patch.object(sync_factory, "assert_temple_shop_active", return_value=True),
                patch.object(sync_factory, "load_shop_config", return_value={}),
            ):
                before = set(shop.iterdir())
                sync_factory.sync_shop("templystudios")
                after = set(shop.iterdir())
                self.assertEqual(before, after)

            with patch.object(sync_factory, "assert_temple_shop_active", return_value=False):
                # Guard should avoid syncing if active shop is not Temply
                sync_factory.sync_shop("templystudios")
                before_count = sum(1 for _ in shop.iterdir())
                self.assertEqual(before_count, len(before))

    def test_sync_watch_fingerprint_tracks_name_size_mtime_and_excludes_non_factory(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, shop, excel = self._prepare_sync_env(base)

            src = source / "ready"
            (src / "etsy-images-chatgpt-final").mkdir(parents=True)
            (src / "source-zip").mkdir(parents=True)
            img = src / "etsy-images-chatgpt-final" / "cover.png"
            zip_file = src / "source-zip" / "ready.zip"
            img.write_bytes(b"one")
            zip_file.write_bytes(b"two")

            excluded = source / "_asset_quarantine" / "etsy-images-chatgpt-final"
            excluded.parent.mkdir(parents=True)
            (source / "_asset_quarantine" / "etsy-images-chatgpt-final").mkdir(parents=True)
            (source / "_asset_quarantine" / "source-zip").mkdir(parents=True)
            (source / "_asset_quarantine" / "etsy-images-chatgpt-final" / "cover.png").write_bytes(b"one")
            (source / "_asset_quarantine" / "source-zip" / "ready.zip").write_bytes(b"two")

            with (
                patch.object(sync_factory, "BASE_DIR", base),
                patch.object(sync_factory, "SRC_DIR", source),
                patch.object(sync_factory, "assert_temple_shop_active", return_value=True),
                patch.object(sync_factory, "load_shop_config", return_value={}),
            ):
                fp1 = sync_factory.get_dir_fingerprint(source)
                self.assertIn("ready", fp1)
                self.assertNotIn("_asset_quarantine", fp1)

                zip_file.write_bytes(b"two-more")
                fp2 = sync_factory.get_dir_fingerprint(source)
                self.assertNotEqual(fp1, fp2)

                # Stable check still succeeds for compatible folders
                self.assertTrue(sync_factory.is_folder_stable(src, wait_secs=1))


if __name__ == "__main__":
    unittest.main()
