from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import dashboard_app


class DashboardCatalogScalingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.products = [
            {"row": 4, "folder": "product-04", "title": "Beta", "tags": "alpha", "status": "⏳ Chờ đăng", "price": 4.0},
            {"row": 5, "folder": "product-05", "title": "Alpha", "tags": "beta", "status": "✅ Đã đăng", "price": 8.0},
            {"row": 6, "folder": "product-06", "title": "Gamma", "tags": "gamma", "status": "❌ Lỗi", "price": 2.0},
        ]
        self.patches = [
            patch.object(dashboard_app, "products_from_excel", return_value=list(self.products)),
            patch.object(dashboard_app, "latest_etsy_manager_snapshot", return_value={"listings": []}),
            patch.object(dashboard_app, "enrich_products_with_etsy_manager", side_effect=lambda products, snapshot: products),
            patch.object(dashboard_app, "attach_local_products_to_etsy_snapshot", side_effect=lambda snapshot, products: {"listings": []}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()

    def _json(self, response):
        return json.loads(response.body.decode("utf-8"))

    def test_default_response_is_backward_compatible(self) -> None:
        payload = self._json(dashboard_app.get_products())
        self.assertEqual(3, len(payload["products"]))
        self.assertNotIn("pagination", payload)

    def test_filter_sort_and_page_are_deterministic(self) -> None:
        payload = self._json(
            dashboard_app.get_products(
                page=1,
                page_size=1,
                q="alpha",
                sort="-price",
            )
        )
        self.assertEqual(2, payload["pagination"]["total"])
        self.assertEqual("Alpha", payload["products"][0]["title"])
        self.assertTrue(payload["pagination"]["has_next"])

        second = self._json(
            dashboard_app.get_products(page=2, page_size=1, q="alpha", sort="-price")
        )
        self.assertEqual("Beta", second["products"][0]["title"])
        self.assertFalse(second["pagination"]["has_next"])

    def test_status_filter_and_invalid_sort_fail_closed(self) -> None:
        payload = self._json(dashboard_app.get_products(page_size=10, status="lỗi"))
        self.assertEqual(["Gamma"], [item["title"] for item in payload["products"]])
        with self.assertRaises(dashboard_app.HTTPException) as raised:
            dashboard_app.get_products(page_size=10, sort="secret")
        self.assertEqual(400, raised.exception.status_code)

    def test_status_filter_matches_new_import_pending_warning_status(self) -> None:
        products = [
            {"row": 4, "folder": "product-04", "title": "Beta", "tags": "alpha", "status": "✅ Đã đăng", "price": 4.0},
            {"row": 5, "folder": "product-05", "title": "Alpha", "tags": "beta", "status": "✅ Đã đăng", "price": 8.0},
            {"row": 6, "folder": "product-06", "title": "Gamma", "tags": "gamma", "status": "❌ Lỗi", "price": 2.0},
            {
                "row": 7,
                "folder": "product-07",
                "title": "Delta",
                "tags": "delta",
                "status": "🆕 Mới import · ⏳ Chờ đăng · ⚠ Cần generate SEO",
                "price": 6.0,
            },
        ]

        with patch.object(dashboard_app, "products_from_excel", return_value=products):
            payload = self._json(dashboard_app.get_products(page_size=10, status="chờ đăng"))
        self.assertEqual(["Delta"], [item["title"] for item in payload["products"]])


if __name__ == "__main__":
    unittest.main()
