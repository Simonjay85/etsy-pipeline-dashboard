"""Regression coverage for all-status snapshots and syncable-only workbook updates."""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl

import etsy_shop_sync


class _ScrapePage:
    def __init__(self, result: list[dict], network_result=None) -> None:
        self.result = result
        self.network_result = network_result if network_result is not None else {}
        self.script = ""
        self.argument = None
        self.scripts = []
        self.arguments = []

    async def evaluate(self, script, argument):
        self.script = script
        self.argument = argument
        self.scripts.append(script)
        self.arguments.append(argument)
        if "performance.getEntriesByType" in script:
            return self.network_result
        return self.result


class TestEtsyShopSyncStatusScope(unittest.TestCase):
    def _make_workbook(self, path: Path, *, listing_url: str = "") -> None:
        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.title = "Listings"
        worksheet.cell(row=4, column=2, value="product-01")
        worksheet.cell(row=4, column=8, value="Only inactive listing")
        worksheet.cell(row=4, column=14, value="⏸ Inactive trên Etsy")
        worksheet.cell(row=4, column=16, value=listing_url or None)
        workbook.save(path)

    def test_inactive_and_expired_are_retained_in_snapshot_but_cannot_remap_workbook(self) -> None:
        crawled = {
            "active": [],
            "draft": [],
            "inactive": [{"id": "901", "title": "Only inactive listing"}],
            "expired": [{"id": "902", "title": "Only expired listing"}],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            shop_dir = root / "shops" / "shop"
            shop_dir.mkdir(parents=True)
            workbook_path = shop_dir / "Etsy_SEO_Generator.xlsx"
            # An existing inactive URL must be preserved, not cleared or
            # replaced through the active/draft reconciliation path.
            self._make_workbook(workbook_path, listing_url="https://www.etsy.com/listing/901")
            (root / "scratch").mkdir()

            with patch.object(etsy_shop_sync, "BASE_DIR", root):
                report = etsy_shop_sync.sync_excel("shop", crawled)

            saved = openpyxl.load_workbook(workbook_path)["Listings"]
            self.assertEqual("⏸ Inactive trên Etsy", saved.cell(row=4, column=14).value)
            self.assertEqual("https://www.etsy.com/listing/901", saved.cell(row=4, column=16).value)
            self.assertEqual(2, report["etsy_total"])
            self.assertEqual(0, report["syncable_etsy_total"])
            self.assertEqual(0, report["matched_total"])
            self.assertEqual(0, report["etsy_unmapped_total"])
            self.assertEqual(
                {"active": 0, "draft": 0, "inactive": 1, "expired": 1},
                report["etsy_counts"],
            )
            snapshot = json.loads(Path(report["crawl"]).read_text(encoding="utf-8"))
            self.assertEqual(crawled, snapshot)

    def test_loaded_listing_date_fields_are_preserved_for_snapshot_and_dashboard(self) -> None:
        listing = {
            "id": "903",
            "title": "Listing with Etsy state date",
            "formatted_state_date": "Aug 30, 2026",
            "state_date": 1788097500,
        }
        crawled = {
            "active": [listing],
            "draft": [],
            "inactive": [],
            "expired": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            shop_dir = root / "shops" / "shop"
            shop_dir.mkdir(parents=True)
            workbook_path = shop_dir / "Etsy_SEO_Generator.xlsx"
            self._make_workbook(workbook_path)
            (root / "scratch").mkdir()

            with patch.object(etsy_shop_sync, "BASE_DIR", root):
                report = etsy_shop_sync.sync_excel("shop", crawled)

            snapshot = json.loads(Path(report["crawl"]).read_text(encoding="utf-8"))
            self.assertEqual("Aug 30, 2026", snapshot["active"][0]["formatted_state_date"])
            self.assertEqual(1788097500, snapshot["active"][0]["state_date"])

        # The browser extractor is intentionally read-only and tolerant: if
        # Etsy changes its markup, the normal listing fields still survive.
        source = inspect.getsource(etsy_shop_sync.scrape_visible_listings)
        self.assertIn("formatted_state_date", source)
        self.assertIn("state_date", source)
        self.assertIn("data-listing-data", source)


class TestEtsyShopSyncDateExtraction(unittest.IsolatedAsyncioTestCase):
    async def test_scrape_visible_listings_carries_loaded_state_date_fields(self) -> None:
        page = _ScrapePage([{
            "id": "904",
            "title": "Listing with formatted state date",
            "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/904",
            "url": "https://www.etsy.com/listing/904",
            "managerStatus": "active",
            "page": 1,
            "formatted_state_date": "Aug 31, 2026",
            "state_date": 1788163200,
        }])

        items = await etsy_shop_sync.scrape_visible_listings(page, "active", 1)

        self.assertEqual("904", items[0]["id"])
        self.assertEqual("Aug 31, 2026", items[0]["formatted_state_date"])
        self.assertEqual(1788163200, items[0]["state_date"])
        self.assertEqual({"status": "active", "pageNum": 1}, page.arguments[0])
        self.assertIn("data-${field}", page.scripts[0])
        self.assertIn("field.replace(/_/g, '-')", page.scripts[0])
        self.assertIn("data-listing-data", page.scripts[0])

    async def test_network_response_fallback_merges_dates_by_listing_id(self) -> None:
        page = _ScrapePage(
            [{
                "id": "905",
                "title": "Listing without date in DOM",
                "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/905",
                "url": "https://www.etsy.com/listing/905",
                "managerStatus": "active",
                "page": 1,
            }],
            network_result={
                "905": {
                    "formatted_state_date": "Aug 31, 2026",
                    "state_date": 1788163200,
                    # Non-date fields must never be merged from the response.
                    "title": "Do not replace DOM title",
                },
            },
        )

        items = await etsy_shop_sync.scrape_visible_listings(page, "active", 1)

        self.assertEqual("Listing without date in DOM", items[0]["title"])
        self.assertEqual("Aug 31, 2026", items[0]["formatted_state_date"])
        self.assertEqual(1788163200, items[0]["state_date"])
        self.assertEqual(2, len(page.scripts))
        self.assertIn("performance.getEntriesByType", page.scripts[1])
        self.assertIn("credentials: 'include'", page.scripts[1])
        self.assertIn("parsed.origin === location.origin", page.scripts[1])
        self.assertIn("if (Array.isArray(payload)) return payload", page.scripts[1])
        self.assertIn("pathParts[4] === 'shop'", page.scripts[1])
        self.assertIn("record.listing_id", page.scripts[1])
        self.assertIn("searchParams.get('state')", page.scripts[1])
        self.assertEqual({"status": "active"}, page.arguments[1])


if __name__ == "__main__":
    unittest.main()
