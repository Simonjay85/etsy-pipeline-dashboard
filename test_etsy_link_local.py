import asyncio
import unittest
from unittest import mock

from fastapi import HTTPException

import dashboard_app


class EtsyLinkLocalTests(unittest.TestCase):
    def test_status_for_linked_etsy_listing_prefers_snapshot(self):
        self.assertEqual(
            dashboard_app._status_for_linked_etsy_listing({"managerStatus": "draft"}, "⏳ Chờ đăng"),
            "✅ Đã đăng draft",
        )
        self.assertEqual(
            dashboard_app._status_for_linked_etsy_listing({"managerStatus": "active"}, "⏳ Chờ đăng"),
            "✅ Đã đăng",
        )

    def test_status_for_linked_etsy_listing_clears_unverified(self):
        self.assertEqual(
            dashboard_app._status_for_linked_etsy_listing(
                None,
                "✅ Đã đăng draft (URL chưa xác minh)",
            ),
            "✅ Đã đăng draft",
        )

    def test_mapped_etsy_listing_ids(self):
        mapped = dashboard_app._mapped_etsy_listing_ids([
            {"etsy_url": "https://www.etsy.com/listing/111"},
            {"etsy_url": ""},
            {"etsy_url": "https://www.etsy.com/listing/222?ref=x"},
        ])
        self.assertEqual(mapped, {"111", "222"})

    def test_link_suggestions_for_folder_ranks_unmapped_drafts(self):
        products = [
            {
                "row": 10,
                "folder": "product-396",
                "title": "Bunny SVG Bundle | Rabbit Silhouette SVG",
                "etsy_url": "",
                "status": "✅ Đã đăng draft (URL chưa xác minh)",
            },
            {
                "row": 11,
                "folder": "product-397",
                "title": "Baseball SVG Bundle",
                "etsy_url": "https://www.etsy.com/listing/4542555210",
                "status": "✅ Đã đăng draft",
            },
        ]
        snapshot = {
            "listings": [
                {
                    "id": "4542555210",
                    "title": "Baseball SVG Bundle",
                    "managerStatus": "draft",
                    "url": "https://www.etsy.com/listing/4542555210",
                },
                {
                    "id": "999000111",
                    "title": "Bunny SVG Bundle | Rabbit Silhouette SVG | Easter",
                    "managerStatus": "draft",
                    "url": "https://www.etsy.com/listing/999000111",
                },
                {
                    "id": "888",
                    "title": "Totally unrelated mug design",
                    "managerStatus": "draft",
                    "url": "https://www.etsy.com/listing/888",
                },
            ]
        }
        with mock.patch.object(dashboard_app, "products_from_excel", return_value=products), \
             mock.patch.object(dashboard_app, "latest_etsy_manager_snapshot", return_value=snapshot):
            payload = asyncio.run(
                dashboard_app.etsy_link_suggestions_for_folder("product-396", limit=3)
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["auto_fill_listing_id"], "999000111")
        self.assertEqual(payload["suggestions"][0]["id"], "999000111")
        self.assertNotIn("4542555210", [item["id"] for item in payload["suggestions"]])

    def test_map_listing_allows_manual_without_snapshot(self):
        products = [{
            "row": 10,
            "folder": "product-396",
            "title": "Bunny SVG Bundle",
            "etsy_url": "",
            "status": "✅ Đã đăng draft (URL chưa xác minh)",
        }]
        saved = {}

        def fake_save(row, updates, excel_path=None):
            saved["row"] = row
            saved["updates"] = updates

        class Request:
            def __init__(self, payload):
                self._payload = payload

            async def json(self):
                return self._payload

        with mock.patch.object(dashboard_app, "products_from_excel", return_value=products), \
             mock.patch.object(dashboard_app, "latest_etsy_manager_snapshot", return_value={"listings": []}), \
             mock.patch.object(dashboard_app, "save_to_excel", side_effect=fake_save), \
             mock.patch.object(dashboard_app, "broadcast"):
            result = asyncio.run(
                dashboard_app.map_etsy_listing(Request({
                    "folder": "product-396",
                    "listing_id": "999000111",
                    "allow_manual": True,
                }))
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["from_snapshot"])
        self.assertEqual(saved["row"], 10)
        self.assertEqual(saved["updates"]["etsy_url"], "https://www.etsy.com/listing/999000111")
        self.assertEqual(saved["updates"]["status"], "✅ Đã đăng draft")

    def test_map_listing_still_requires_snapshot_without_manual_flag(self):
        products = [{
            "row": 10,
            "folder": "product-396",
            "title": "Bunny SVG Bundle",
            "etsy_url": "",
            "status": "✅ Đã đăng draft (URL chưa xác minh)",
        }]

        class Request:
            def __init__(self, payload):
                self._payload = payload

            async def json(self):
                return self._payload

        with mock.patch.object(dashboard_app, "products_from_excel", return_value=products), \
             mock.patch.object(dashboard_app, "latest_etsy_manager_snapshot", return_value={"listings": []}):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    dashboard_app.map_etsy_listing(Request({
                        "folder": "product-396",
                        "listing_id": "999000111",
                    }))
                )
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
