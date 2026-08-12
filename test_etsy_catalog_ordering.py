#!/usr/bin/env python3
"""Focused regression tests for natural dashboard catalog ordering."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etsy_catalog import build_unified_catalog, load_local_catalog


class TestEtsyCatalogOrdering(unittest.TestCase):
    def test_local_product_folders_sort_by_numeric_suffix(self) -> None:
        workbook_rows = {
            "product-469": {"row": 4, "title": "Later product"},
            "product-47": {"row": 5, "title": "Earlier product"},
        }

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "etsy_catalog._load_workbook_rows", return_value=workbook_rows
        ):
            shop_dir = Path(tmpdir) / "shops" / "test-shop"
            (shop_dir / "product-469").mkdir(parents=True)
            (shop_dir / "product-47").mkdir()
            records = load_local_catalog(Path(tmpdir), "test-shop", Path(tmpdir) / "catalog.xlsx")

        self.assertEqual(["product-47", "product-469"], [record["folder"] for record in records])

    def test_workbook_row_without_physical_folder_is_not_local(self) -> None:
        workbook_rows = {
            "product-246": {
                "row": 354,
                "title": "product-246",
                "etsy_url": "",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "etsy_catalog._load_workbook_rows", return_value=workbook_rows
        ):
            records = load_local_catalog(Path(tmpdir), "test-shop", Path(tmpdir) / "catalog.xlsx")

        self.assertEqual([], records)

    def test_physical_folder_without_workbook_row_remains_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "etsy_catalog._load_workbook_rows", return_value={}
        ):
            folder = Path(tmpdir) / "shops" / "test-shop" / "product-12"
            folder.mkdir(parents=True)
            records = load_local_catalog(Path(tmpdir), "test-shop", Path(tmpdir) / "catalog.xlsx")

        self.assertEqual(["product-12"], [record["folder"] for record in records])
        self.assertTrue(records[0]["exists"])
        self.assertIsNone(records[0]["row"])

    def test_mapped_workbook_row_without_folder_remains_etsy_only(self) -> None:
        workbook_rows = {
            "product-246": {
                "row": 354,
                "title": "Missing local product",
                "etsy_url": "https://www.etsy.com/listing/24600",
            },
        }
        snapshot = {
            "source": "snapshot.json",
            "listings": [
                {"id": "24600", "title": "Remote product", "managerStatus": "active"},
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "etsy_catalog._load_workbook_rows", return_value=workbook_rows
        ), patch("etsy_catalog.load_etsy_snapshot", return_value=snapshot):
            catalog = build_unified_catalog(Path(tmpdir), "test-shop", Path(tmpdir) / "catalog.xlsx")

        self.assertEqual(0, catalog["counts"]["local_total"])
        self.assertEqual(0, catalog["counts"]["mapped_total"])
        self.assertEqual(1, catalog["counts"]["etsy_only_total"])
        self.assertEqual("etsy", catalog["records"][0]["source"])
        self.assertEqual("", catalog["records"][0]["folder"])

    def test_unified_catalog_sorts_mapped_and_local_before_etsy_only(self) -> None:
        local_records = [
            {
                "record_id": "local:test-shop:product-469",
                "source": "local",
                "folder": "product-469",
                "listing_id": "",
                "normalized_title": "",
                "asset_hashes": [],
            },
            {
                "record_id": "local:test-shop:product-47",
                "source": "local",
                "folder": "product-47",
                "listing_id": "900",
                "normalized_title": "",
                "asset_hashes": [],
                "etsy_url": "",
            },
        ]
        snapshot = {
            "source": "snapshot.json",
            "listings": [
                {"id": "10", "title": "Remote ten", "managerStatus": "active"},
                {"id": "900", "title": "Mapped product", "managerStatus": "active"},
                {"id": "2", "title": "Remote two", "managerStatus": "draft"},
            ],
        }

        with patch("etsy_catalog.load_local_catalog", return_value=local_records), patch(
            "etsy_catalog.load_etsy_snapshot", return_value=snapshot
        ):
            catalog = build_unified_catalog(Path("/unused"), "test-shop", Path("/unused/catalog.xlsx"))

        self.assertEqual(
            [
                ("product-47", "900", "both"),
                ("product-469", "", "local"),
                ("", "2", "etsy"),
                ("", "10", "etsy"),
            ],
            [
                (record["folder"], record["listing_id"], record["source"])
                for record in catalog["records"]
            ],
        )

    def test_shared_listing_keeps_each_physical_local_and_duplicate_detection(self) -> None:
        shared_asset = {"sha256": "same-signature", "name": "shared.pdf"}
        local_records = [
            {
                "record_id": f"local:test-shop:{folder}",
                "source": "local",
                "folder": folder,
                "listing_id": "4434249871",
                "normalized_title": "shared physical product",
                "asset_hashes": [shared_asset],
                "etsy_url": "",
            }
            for folder in ("product-243", "product-22")
        ]
        snapshot = {
            "source": "snapshot.json",
            "listings": [
                {"id": "4434249871", "title": "Shared listing", "managerStatus": "active"},
            ],
        }

        with patch("etsy_catalog.load_local_catalog", return_value=local_records), patch(
            "etsy_catalog.load_etsy_snapshot", return_value=snapshot
        ):
            catalog = build_unified_catalog(Path("/unused"), "test-shop", Path("/unused/catalog.xlsx"))

        self.assertEqual(["product-22", "product-243"], [record["folder"] for record in catalog["records"]])
        self.assertEqual(["both", "both"], [record["source"] for record in catalog["records"]])
        self.assertEqual(2, len({record["record_id"] for record in catalog["records"]}))
        self.assertEqual(2, catalog["counts"]["mapped_total"])
        self.assertEqual(1, catalog["counts"]["mapped_listing_total"])
        self.assertEqual([["product-22", "product-243"]], [group["folders"] for group in catalog["duplicate_groups"]])


if __name__ == "__main__":
    unittest.main()
