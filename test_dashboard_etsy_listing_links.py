#!/usr/bin/env python3
"""Focused regression tests for status-aware Etsy dashboard links."""

from __future__ import annotations

import unittest

import dashboard_app


class TestDashboardEtsyListingLinks(unittest.TestCase):
    def test_enrichment_uses_exact_snapshot_id_and_fails_closed(self) -> None:
        snapshot = {
            "source": "/tmp/etsy_manager_current_shop_20260731_010000.json",
            "freshness": {
                "source": "/tmp/etsy_manager_current_shop_20260731_010000.json",
                "snapshotAt": "2026-07-31T01:00:00",
                "stale": False,
            },
            "listings": [
                {
                    "id": "100",
                    "managerStatus": "active",
                    "url": "https://www.etsy.com/listing/100",
                    "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/100",
                },
                {
                    "id": "101",
                    "managerStatus": "draft",
                    "url": "https://www.etsy.com/listing/101",
                    "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/101",
                },
                {
                    "id": "102",
                    "managerStatus": "inactive",
                    "url": "https://www.etsy.com/listing/102",
                    "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/102",
                },
                {
                    "id": "103",
                    "managerStatus": "expired",
                    "url": "https://www.etsy.com/listing/103",
                    "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/103",
                },
                {"id": "104", "managerStatus": "draft", "url": "https://www.etsy.com/listing/104"},
            ],
        }
        products = [
            {"folder": "product-100", "etsy_url": "https://www.etsy.com/listing/100"},
            {"folder": "product-101", "etsy_url": "https://www.etsy.com/listing/101"},
            {"folder": "product-102", "etsy_url": "https://www.etsy.com/listing/102"},
            {"folder": "product-103", "etsy_url": "https://www.etsy.com/listing/103"},
            {"folder": "product-104", "etsy_url": "https://www.etsy.com/listing/104"},
            {"folder": "product-999", "etsy_url": "https://www.etsy.com/listing/999", "status": "⏳ Chờ đăng"},
        ]

        result = dashboard_app.enrich_products_with_etsy_manager(products, snapshot)
        by_folder = {item["folder"]: item for item in result}

        self.assertEqual("active", by_folder["product-100"]["etsy_manager_status"])
        self.assertEqual("https://www.etsy.com/listing/100", by_folder["product-100"]["etsy_public_url"])
        self.assertIsNone(by_folder["product-100"]["etsy_manage_url"])

        for folder, listing_id in (("product-101", "101"), ("product-102", "102"), ("product-103", "103")):
            self.assertIsNone(by_folder[folder]["etsy_public_url"])
            self.assertEqual(
                f"https://www.etsy.com/your/shops/me/listing-editor/edit/{listing_id}",
                by_folder[folder]["etsy_manage_url"],
            )
            self.assertEqual("manager", by_folder[folder]["etsy_link_type"])

        self.assertIsNone(by_folder["product-104"]["etsy_public_url"])
        self.assertIsNone(by_folder["product-104"]["etsy_manage_url"])
        self.assertEqual("manager_url_missing", by_folder["product-104"]["etsy_link_unavailable_reason"])

        self.assertIsNone(by_folder["product-999"]["etsy_manage_url"])
        self.assertIsNone(by_folder["product-999"]["etsy_public_url"])
        self.assertEqual("unavailable", by_folder["product-999"]["etsy_link_type"])
        self.assertFalse(by_folder["product-999"]["etsy_link_verified"])
        self.assertEqual("listing_not_in_snapshot", by_folder["product-999"]["etsy_link_unavailable_reason"])
        self.assertIsNone(by_folder["product-999"]["etsy_link_warning_reason"])

        # The workbook value remains the mapping source and is not rewritten.
        self.assertEqual("https://www.etsy.com/listing/101", by_folder["product-101"]["etsy_url"])
        self.assertFalse(by_folder["product-101"]["etsy_snapshot_stale"])

    def test_missing_locale_public_listing_id_is_exposed_as_local_unverified_link(self) -> None:
        snapshot = {
            "source": "/tmp/etsy_manager_current_shop_20260731_010000.json",
            "freshness": {
                "source": "/tmp/etsy_manager_current_shop_20260731_010000.json",
                "snapshotAt": "2026-07-31T01:00:00",
                "stale": False,
            },
            "listings": [],
        }
        products = [{
            "folder": "product-locale",
            "etsy_url": "https://www.etsy.com/ca/listing/4555695025/800-ai-commands-for-etsy-sellers-a?ref=listings_manager_grid",
            "status": "✅ Đã đăng",
        }]

        result = dashboard_app.enrich_products_with_etsy_manager(products, snapshot)
        self.assertEqual("local_unverified", result[0]["etsy_link_type"])
        self.assertEqual(
            "https://www.etsy.com/ca/listing/4555695025/800-ai-commands-for-etsy-sellers-a?ref=listings_manager_grid",
            result[0]["etsy_public_url"],
        )
        self.assertFalse(result[0]["etsy_link_verified"])
        self.assertEqual("listing_not_in_snapshot", result[0]["etsy_link_unavailable_reason"])
        self.assertEqual("listing_not_in_snapshot", result[0]["etsy_link_warning_reason"])
        self.assertFalse(result[0]["etsy_snapshot_stale"])


if __name__ == "__main__":
    unittest.main()
