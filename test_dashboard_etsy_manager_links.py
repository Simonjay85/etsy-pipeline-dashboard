#!/usr/bin/env python3
"""Focused tests for read-only Etsy Manager link decoration."""

from __future__ import annotations

import unittest

from dashboard_app import enrich_products_with_etsy_manager


class TestDashboardEtsyManagerLinks(unittest.TestCase):
    def test_exact_non_active_match_exposes_manager_link_without_changing_excel_url(self) -> None:
        product = {"folder": "product-03", "etsy_url": "https://www.etsy.com/listing/123"}
        snapshot = {
            "freshness": {"snapshotAt": "2026-07-23T23:45:14", "source": "snapshot.json", "stale": False},
            "listings": [{
                "id": "123",
                "managerStatus": "expired",
                "url": "https://www.etsy.com/listing/123",
                "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/123",
            }],
        }

        enriched = enrich_products_with_etsy_manager([product], snapshot)[0]

        self.assertEqual(product["etsy_url"], "https://www.etsy.com/listing/123")
        self.assertEqual(enriched["etsy_manager_status"], "expired")
        self.assertEqual(enriched["etsy_edit_url"], "https://www.etsy.com/your/shops/me/listing-editor/edit/123")
        self.assertEqual(enriched["etsy_manage_url"], enriched["etsy_edit_url"])
        self.assertEqual(enriched["etsy_link_type"], "manager")

    def test_active_match_exposes_public_link_and_not_manager_link(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://www.etsy.com/listing/456"}],
            {"listings": [{
                "id": "456",
                "managerStatus": "active",
                "url": "https://www.etsy.com/listing/456",
                "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/456",
            }]},
        )[0]

        self.assertEqual(enriched["etsy_manager_status"], "active")
        self.assertEqual(enriched["etsy_public_url"], "https://www.etsy.com/listing/456")
        self.assertEqual(enriched["etsy_edit_url"], "https://www.etsy.com/your/shops/me/listing-editor/edit/456")
        self.assertEqual(enriched["etsy_link_type"], "public")

    def test_unknown_status_exposes_metadata_but_does_not_select_manager_link(self) -> None:
        enriched = enrich_products_with_etsy_manager(
            [{"etsy_url": "https://www.etsy.com/listing/789"}],
            {"listings": [{
                "id": "789",
                "managerStatus": "archived",
                "editUrl": "https://www.etsy.com/your/shops/me/listing-editor/edit/789",
            }]},
        )[0]

        self.assertEqual(enriched["etsy_manager_status"], "archived")
        self.assertEqual(enriched["etsy_edit_url"], "https://www.etsy.com/your/shops/me/listing-editor/edit/789")
        self.assertEqual(enriched["etsy_link_type"], "unavailable")


if __name__ == "__main__":
    unittest.main()
