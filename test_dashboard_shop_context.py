#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard_app


class DashboardShopContextTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(dashboard_app.app, base_url="http://127.0.0.1:8090")
        self.shops = {
            "shop-a": {"id": "shop-a", "name": "Shop A"},
            "shop-b": {"id": "shop-b", "name": "Shop B"},
        }

    def test_set_shop_rejects_missing_and_unknown_ids_without_touching_active_file(self):
        with tempfile.TemporaryDirectory() as temp:
            active_file = Path(temp) / "active_shop.txt"
            active_file.write_text("shop-a", encoding="utf-8")
            with patch.object(dashboard_app, "SHOPS", self.shops), \
                    patch.object(dashboard_app, "_active_shop_id", "shop-a"), \
                    patch.object(dashboard_app, "ACTIVE_SHOP_FILE", active_file), \
                    patch.object(dashboard_app, "_DASHBOARD_MUTATION_TOKEN", "test-token"):
                headers = {dashboard_app._DASHBOARD_MUTATION_TOKEN_HEADER: "test-token"}
                missing = self.client.post("/api/set-shop", json={}, headers=headers)
                unknown = self.client.post("/api/set-shop", json={"shop_id": "no-such-shop"}, headers=headers)
                self.assertEqual(missing.status_code, 400)
                self.assertEqual(unknown.status_code, 400)
                self.assertEqual(active_file.read_text(encoding="utf-8"), "shop-a")

    def test_set_shop_returns_exact_authoritative_active_id(self):
        with tempfile.TemporaryDirectory() as temp:
            active_file = Path(temp) / "active_shop.txt"
            with patch.object(dashboard_app, "SHOPS", self.shops), \
                    patch.object(dashboard_app, "_active_shop_id", "shop-a"), \
                    patch.object(dashboard_app, "ACTIVE_SHOP_FILE", active_file), \
                    patch.object(dashboard_app, "_DASHBOARD_MUTATION_TOKEN", "test-token"):
                response = self.client.post(
                    "/api/set-shop", json={"shop_id": "shop-b"},
                    headers={dashboard_app._DASHBOARD_MUTATION_TOKEN_HEADER: "test-token"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["ok"], True)
                self.assertEqual(response.json()["active"], "shop-b")
                self.assertEqual(response.json()["shop"]["id"], "shop-b")
                self.assertEqual(active_file.read_text(encoding="utf-8"), "shop-b")

    def test_products_include_active_shop_for_unpaged_and_paged_responses(self):
        products = [{"row": 1, "folder": "product-01", "title": "One", "status": "pending"}]
        with patch.object(dashboard_app, "SHOPS", self.shops), \
                patch.object(dashboard_app, "_active_shop_id", "shop-b"), \
                patch.object(dashboard_app, "products_from_excel", return_value=products), \
                patch.object(dashboard_app, "latest_etsy_manager_snapshot", return_value={}), \
                patch.object(dashboard_app, "enrich_products_with_etsy_manager", side_effect=lambda p, _s: p):
            unpaged = self.client.get("/api/products")
            paged = self.client.get("/api/products?page=1&page_size=1")
        self.assertEqual(unpaged.status_code, 200)
        self.assertEqual(paged.status_code, 200)
        self.assertEqual(unpaged.json()["active_shop"], {"id": "shop-b", "name": "Shop B"})
        self.assertEqual(paged.json()["active_shop"], {"id": "shop-b", "name": "Shop B"})

    def test_products_fail_closed_if_shop_changes_during_response_construction(self):
        products = [{"row": 1, "folder": "product-01", "title": "One", "status": "pending"}]

        def switch_shop(product_rows, _snapshot):
            dashboard_app._active_shop_id = "shop-b"
            return product_rows

        with patch.object(dashboard_app, "SHOPS", self.shops), \
                patch.object(dashboard_app, "_active_shop_id", "shop-a"), \
                patch.object(dashboard_app, "products_from_excel", return_value=products), \
                patch.object(dashboard_app, "latest_etsy_manager_snapshot", return_value={}), \
                patch.object(dashboard_app, "enrich_products_with_etsy_manager", side_effect=switch_shop):
            unpaged = self.client.get("/api/products")
        self.assertEqual(unpaged.status_code, 409)
        self.assertIn("Shop context changed", unpaged.json()["detail"])


if __name__ == "__main__":
    unittest.main()
