#!/usr/bin/env python3
"""Focused regression tests for natural dashboard catalog ordering."""

from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from etsy_catalog import build_unified_catalog, load_etsy_snapshot, load_local_catalog, normalize_etsy_manager_snapshot


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

    def test_unified_catalog_keeps_unmatched_active_and_draft_etsy_listings(self) -> None:
        local_records = [
            {
                "record_id": "local:test-shop:product-111",
                "source": "local",
                "folder": "product-111",
                "listing_id": "",
                "normalized_title": "active local",
                "asset_hashes": [],
            },
        ]
        snapshot = {
            "source": "snapshot.json",
            "listings": [
                {"id": "100", "title": "Active listing", "managerStatus": "active"},
                {"id": "101", "title": "Draft listing", "managerStatus": "draft"},
                {"id": "102", "title": "Inactive listing", "managerStatus": "inactive"},
            ],
        }

        with patch("etsy_catalog.load_local_catalog", return_value=local_records), patch(
            "etsy_catalog.load_etsy_snapshot", return_value=snapshot
        ):
            catalog = build_unified_catalog(Path("/unused"), "test-shop", Path("/unused/catalog.xlsx"))

        etsy_records = [record for record in catalog["records"] if record["source"] == "etsy"]
        self.assertEqual(2, len(etsy_records))
        self.assertEqual({"100", "101"}, {record["listing_id"] for record in etsy_records})
        self.assertEqual(1, catalog["counts"]["etsy_only_hidden_non_syncable_total"])
        self.assertEqual(2, catalog["counts"]["etsy_only_total"])
        self.assertEqual(3, catalog["counts"]["etsy_total"])
        self.assertEqual(3, catalog["counts"]["unified_total"])
        self.assertEqual(1, catalog["counts"]["local_only_total"])

    def test_unified_catalog_hides_non_syncable_unmapped_etsy_listings(self) -> None:
        local_records: list[dict[str, object]] = []
        snapshot = {
            "source": "snapshot.json",
            "listings": [
                {"id": "201", "title": "Expired listing", "managerStatus": "expired"},
                {"id": "202", "title": "Unknown status listing", "managerStatus": ""},
                {"id": "203", "title": "Mystery listing", "managerStatus": "archived"},
            ],
        }

        with patch("etsy_catalog.load_local_catalog", return_value=local_records), patch(
            "etsy_catalog.load_etsy_snapshot", return_value=snapshot
        ):
            catalog = build_unified_catalog(Path("/unused"), "test-shop", Path("/unused/catalog.xlsx"))

        self.assertEqual(0, len([record for record in catalog["records"] if record["source"] == "etsy"]))
        self.assertEqual(0, catalog["counts"]["etsy_only_total"])
        self.assertEqual(3, catalog["counts"]["etsy_only_hidden_non_syncable_total"])
        self.assertEqual(3, catalog["counts"]["etsy_total"])
        self.assertEqual(0, catalog["counts"]["unified_total"])

    def test_local_only_records_are_kept_even_when_unmatched_etsy_listings_hidden(self) -> None:
        local_records = [
            {
                "record_id": "local:test-shop:product-777",
                "source": "local",
                "folder": "product-777",
                "listing_id": "",
                "normalized_title": "local only",
                "asset_hashes": [],
            },
        ]
        snapshot = {
            "source": "snapshot.json",
            "listings": [
                {"id": "301", "title": "Expired listing", "managerStatus": "expired"},
            ],
        }

        with patch("etsy_catalog.load_local_catalog", return_value=local_records), patch(
            "etsy_catalog.load_etsy_snapshot", return_value=snapshot
        ):
            catalog = build_unified_catalog(Path("/unused"), "test-shop", Path("/unused/catalog.xlsx"))

        self.assertEqual(1, catalog["counts"]["local_only_total"])
        self.assertEqual(0, catalog["counts"]["etsy_only_total"])
        self.assertEqual(1, catalog["counts"]["etsy_only_hidden_non_syncable_total"])
        self.assertEqual(1, catalog["counts"]["unified_total"])
        self.assertEqual("product-777", catalog["records"][0]["folder"])

    def test_local_listing_absent_from_snapshot_is_marked_for_reconciliation(self) -> None:
        local_records = [
            {
                "record_id": "local:test-shop:product-777",
                "source": "local",
                "folder": "product-777",
                "listing_id": "777",
                "etsy_url": "https://www.etsy.com/listing/777",
                "status": "✅ Đã đăng draft",
                "normalized_title": "local only",
                "asset_hashes": [],
            },
        ]
        snapshot = {
            "source": "snapshot.json",
            "listings": [
                {"id": "888", "title": "Other listing", "managerStatus": "draft"},
            ],
        }

        with patch("etsy_catalog.load_local_catalog", return_value=local_records), patch(
            "etsy_catalog.load_etsy_snapshot", return_value=snapshot
        ):
            catalog = build_unified_catalog(Path("/unused"), "test-shop", Path("/unused/catalog.xlsx"))

        self.assertEqual(1, catalog["counts"]["local_only_total"])
        local_record = next(record for record in catalog["records"] if record["source"] == "local")
        self.assertEqual("✅ Đã đăng draft", local_record["status"])
        self.assertEqual("unmatched_local_listing", local_record["reconciliation_status"])
        self.assertIn("absent", local_record["reconciliation_note"])

    def test_mapped_inactive_or_expired_listings_still_shown_as_both(self) -> None:
        local_records = [
            {
                "record_id": "local:test-shop:product-333",
                "source": "local",
                "folder": "product-333",
                "listing_id": "333",
                "normalized_title": "mapped inactive",
                "asset_hashes": [],
                "etsy_url": "https://www.etsy.com/listing/333",
            },
        ]
        snapshot = {
            "source": "snapshot.json",
            "listings": [
                {"id": "333", "title": "Mapped inactive", "managerStatus": "inactive"},
            ],
        }

        with patch("etsy_catalog.load_local_catalog", return_value=local_records), patch(
            "etsy_catalog.load_etsy_snapshot", return_value=snapshot
        ):
            catalog = build_unified_catalog(Path("/unused"), "test-shop", Path("/unused/catalog.xlsx"))

        self.assertEqual(1, len(catalog["records"]))
        self.assertEqual("both", catalog["records"][0]["source"])
        self.assertEqual(0, catalog["counts"]["etsy_only_total"])
        self.assertEqual(0, catalog["counts"]["etsy_only_hidden_non_syncable_total"])
        self.assertEqual(1, catalog["counts"]["etsy_total"])
        self.assertEqual(1, catalog["counts"]["unified_total"])
        self.assertEqual(1, catalog["counts"]["mapped_total"])
        self.assertEqual(1, catalog["counts"]["mapped_listing_total"])

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

    def test_normalize_etsy_manager_snapshot_prefers_active_and_preserves_malformed_ids(self) -> None:
        raw_snapshot = {
            "active": [
                {"id": "4511087962", "title": "Active first"},
                {"id": "4511087962", "title": "Active duplicate"},
                {"id": "   451 ", "title": "Active trimmed"},
                {"id": "", "title": "Active missing id"},
                {"id": "not-a-number", "title": "Active malformed id"},
            ],
            "draft": [
                {"id": "4511087962", "title": "Draft duplicate should be ignored"},
                {"id": "777", "title": "Draft unique"},
            ],
            "inactive": [
                {"id": "777", "title": "Inactive duplicate should be ignored"},
                {"id": "222", "title": "Inactive unique"},
            ],
            "expired": [
                {"id": "222", "title": "Expired duplicate should be ignored"},
                {"id": "999", "title": "Expired unique"},
            ],
        }
        normalized = normalize_etsy_manager_snapshot(raw_snapshot)

        self.assertEqual({"active": 5, "draft": 2, "inactive": 2, "expired": 2}, normalized["raw_counts"])
        self.assertEqual({"active": 4, "draft": 1, "inactive": 1, "expired": 1}, normalized["counts"])
        self.assertEqual(4, normalized["duplicate_count"])
        self.assertEqual(7, len(normalized["listings"]))

        listing_map = {listing["listing_id"]: listing["managerStatus"] for listing in normalized["listings"]}
        self.assertEqual("active", listing_map["4511087962"])
        self.assertEqual("active", listing_map["451"])
        self.assertEqual("draft", listing_map["777"])
        self.assertEqual("inactive", listing_map["222"])
        self.assertEqual("expired", listing_map["999"])
        self.assertEqual(1, len([listing for listing in normalized["listings"] if listing["listing_id"] == "451"]))
        self.assertEqual(1, len([listing for listing in normalized["listings"] if listing["listing_id"] == ""]))

    def test_load_etsy_snapshot_adds_snapshot_diagnostics(self) -> None:
        raw_snapshot = {
            "active": [{"id": "11", "title": "Active"}],
            "draft": [{"id": "11", "title": "Draft duplicate"}],
            "inactive": [{"id": "12", "title": "Inactive"}],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            scratch_dir = Path(tmpdir) / "scratch"
            scratch_dir.mkdir(parents=True)
            snapshot_path = scratch_dir / "etsy_manager_current_shop_20260101_010101.json"
            snapshot_path.write_text(json.dumps(raw_snapshot), encoding="utf-8")

            snapshot = load_etsy_snapshot(Path(tmpdir), "shop")

        self.assertEqual({"active": 1, "draft": 0, "inactive": 1, "expired": 0, "total": 2}, snapshot["counts"])
        self.assertEqual({"active": 1, "draft": 1, "inactive": 1, "expired": 0, "total": 3}, snapshot["raw_counts"])
        self.assertEqual(1, snapshot["duplicate_count"])
        self.assertEqual(2, snapshot["counts"]["total"])
        self.assertEqual(3, snapshot["raw_counts"]["total"])


if __name__ == "__main__":
    unittest.main()
