import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl
from starlette.requests import Request

import dashboard_app


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


class ImageFactoryImportTests(unittest.TestCase):
    def test_scan_is_shop_scoped_and_reports_both_statuses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "master_products"
            shop = root / "shops" / "daisyflowdigital"
            source.mkdir(parents=True)
            shop.mkdir(parents=True)
            excel = shop / "Etsy_SEO_Generator.xlsx"
            _make_workbook(excel, [(4, "product-40", "Different keyword")])

            empty = source / "product-01"
            empty.mkdir()
            imported = source / "product-03" / "files"
            imported.mkdir(parents=True)
            (imported / "Exact Product.pdf").write_bytes(b"source")
            pending = source / "product-04" / "files"
            pending.mkdir(parents=True)
            (pending / "New Product.zip").write_bytes(b"source")
            existing_files = shop / "product-40" / "files"
            existing_files.mkdir(parents=True)
            (existing_files / "Exact Product.pdf").write_bytes(b"existing")
            quarantine_files = shop / ".quarantine-old" / "files"
            quarantine_files.mkdir(parents=True)
            (quarantine_files / "New Product.zip").write_bytes(b"orphan")

            folders = dashboard_app._scan_factory_folders(source, shop, excel)

            self.assertEqual([item["name"] for item in folders], ["product-03", "product-04"])
            self.assertTrue(folders[0]["already_imported"])
            self.assertEqual(folders[0]["imported_folder"], "product-40")
            self.assertEqual(folders[0]["matched_by"], "download_file_set")
            self.assertFalse(folders[1]["already_imported"])

    def test_scan_treats_polluted_target_file_set_as_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "master_products"
            shop = root / "shops" / "daisyflowdigital"
            source_files = source / "product-04" / "files"
            target_files = shop / "product-02" / "files"
            source_files.mkdir(parents=True)
            target_files.mkdir(parents=True)
            (source_files / "Kawaii Planner.pdf").write_bytes(b"source")
            (target_files / "Kawaii Planner.pdf").write_bytes(b"factory-copy")
            (target_files / "Old Legitimate Product.zip").write_bytes(b"old-product")
            excel = shop / "Etsy_SEO_Generator.xlsx"
            _make_workbook(excel, [(4, "product-02", "Unrelated old keyword")])

            folders = dashboard_app._scan_factory_folders(source, shop, excel)

            self.assertEqual(1, len(folders))
            self.assertFalse(folders[0]["already_imported"])
            self.assertIsNone(folders[0]["imported_folder"])

    def test_import_rechecks_duplicates_without_mutating_shop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "master_products"
            shop = root / "shops" / "daisyflowdigital"
            files = source / "product-03" / "files"
            files.mkdir(parents=True)
            shop.mkdir(parents=True)
            (files / "Exact Product.pdf").write_bytes(b"source")
            existing_files = shop / "product-40" / "files"
            existing_files.mkdir(parents=True)
            (existing_files / "Exact Product.pdf").write_bytes(b"existing")
            excel = shop / "Etsy_SEO_Generator.xlsx"
            _make_workbook(excel, [(4, "product-40", "Different keyword")])

            with (
                patch.object(dashboard_app, "IMAGE_FACTORY_OUTPUT", source),
                patch.object(dashboard_app, "SHOP_DIR", return_value=shop),
                patch.object(dashboard_app, "EXCEL_FILE", return_value=excel),
                patch.object(dashboard_app, "get_active_shop", return_value={"name": "Daisy Flow Digital"}),
                patch.object(dashboard_app, "_active_shop_id", "daisyflowdigital"),
            ):
                result = asyncio.run(dashboard_app.import_from_factory(_request({
                    "folders": ["product-03"],
                    "auto_seo": False,
                })))

            self.assertTrue(result["ok"])
            self.assertEqual(result["imported"], 0)
            self.assertTrue(result["results"][0]["already_imported"])
            self.assertEqual(result["results"][0]["imported_folder"], "product-40")
            self.assertEqual(sorted(p.name for p in shop.iterdir()), ["Etsy_SEO_Generator.xlsx", "product-40"])

    def test_source_folder_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "master_products"
            (source / "product-03").mkdir(parents=True)
            with patch.object(dashboard_app, "IMAGE_FACTORY_OUTPUT", source):
                self.assertEqual(dashboard_app._resolve_factory_source_folder("product-03"), source / "product-03")
                self.assertIsNone(dashboard_app._resolve_factory_source_folder("../product-03"))
                self.assertIsNone(dashboard_app._resolve_factory_source_folder("missing"))

    def test_import_seeds_column_d_with_factory_keyword(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "master_products"
            shop = root / "shops" / "daisyflowdigital"
            files = source / "product-04" / "files"
            files.mkdir(parents=True)
            shop.mkdir(parents=True)
            (files / "Kawaii-Planner.pdf").write_bytes(b"source")
            excel = shop / "Etsy_SEO_Generator.xlsx"
            _make_workbook(excel)

            with (
                patch.object(dashboard_app, "IMAGE_FACTORY_OUTPUT", source),
                patch.object(dashboard_app, "SHOP_DIR", return_value=shop),
                patch.object(dashboard_app, "EXCEL_FILE", return_value=excel),
                patch.object(dashboard_app, "get_active_shop", return_value={"name": "Daisy Flow Digital"}),
                patch.object(dashboard_app, "_active_shop_id", "daisyflowdigital"),
            ):
                result = asyncio.run(dashboard_app.import_from_factory(_request({
                    "folders": ["product-04"], "auto_seo": False,
                })))

            self.assertEqual(1, result["imported"])
            wb = openpyxl.load_workbook(excel, data_only=True)
            self.assertEqual("Kawaii Planner", wb["Listings"].cell(row=4, column=4).value)
            wb.close()

    def test_import_preserves_partial_legacy_row_and_uses_fully_empty_row(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "master_products"
            shop = root / "shops" / "daisyflowdigital"
            files = source / "product-04" / "files"
            files.mkdir(parents=True)
            shop.mkdir(parents=True)
            (files / "New Product.pdf").write_bytes(b"source")
            excel = shop / "Etsy_SEO_Generator.xlsx"
            _make_workbook(excel)
            wb = openpyxl.load_workbook(excel)
            ws = wb["Listings"]
            ws.cell(row=4, column=3, value="legacy keyword")
            ws.cell(row=4, column=14, value="legacy status")
            ws.cell(row=6, column=2, value="product-legacy")
            wb.save(excel)
            wb.close()

            with (
                patch.object(dashboard_app, "IMAGE_FACTORY_OUTPUT", source),
                patch.object(dashboard_app, "SHOP_DIR", return_value=shop),
                patch.object(dashboard_app, "EXCEL_FILE", return_value=excel),
                patch.object(dashboard_app, "get_active_shop", return_value={"name": "Daisy Flow Digital"}),
                patch.object(dashboard_app, "_active_shop_id", "daisyflowdigital"),
            ):
                result = asyncio.run(dashboard_app.import_from_factory(_request({
                    "folders": ["product-04"], "auto_seo": False,
                })))

            self.assertEqual(5, result["results"][0]["row"])
            wb = openpyxl.load_workbook(excel, data_only=True)
            ws = wb["Listings"]
            self.assertIsNone(ws.cell(row=4, column=2).value)
            self.assertEqual("legacy keyword", ws.cell(row=4, column=3).value)
            self.assertEqual("legacy status", ws.cell(row=4, column=14).value)
            self.assertEqual("New Product", ws.cell(row=5, column=4).value)
            wb.close()


if __name__ == "__main__":
    unittest.main()
