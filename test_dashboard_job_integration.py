#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import dashboard_app
from job_store import JobStore


class DashboardJobIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="etsy-dashboard-job-int-")
        self._store_path = Path(self._tempdir.name) / "jobs.sqlite"
        self._client = TestClient(dashboard_app.app, base_url="http://127.0.0.1:8090")
        self._store = JobStore(self._store_path)
        self._token = "integration-token"

    def tearDown(self) -> None:
        self._store.close()
        self._tempdir.cleanup()
        dashboard_app._etsy_update_jobs.clear()
        dashboard_app._running_processes.clear()
        dashboard_app._running_tasks.clear()

    def _headers(self) -> dict[str, str]:
        return {
            dashboard_app._DASHBOARD_MUTATION_TOKEN_HEADER: self._token,
            "Origin": "http://127.0.0.1:8090",
            "Host": "127.0.0.1:8090",
        }

    def test_push_route_admits_context_and_persists_status_via_store(self) -> None:
        payload: dict[str, Any] = {
            "shop": "templystudios",
            "folder": "product-01",
            "listing_id": "1234567890",
            "fields": ["title", "description"],
            "request_id": "itest-001",
        }

        async def no_op_sync(*args: Any, **kwargs: Any) -> None:
            return None

        def fake_store_factory() -> JobStore:
            return self._store

        with patch.object(
            dashboard_app, "_active_shop_id", "templystudios"
        ), patch.object(
            dashboard_app, "get_product_by_row",
            return_value={"folder": "product-01", "etsy_url": "https://www.etsy.com/listing/1234567890"},
        ), patch.object(
            dashboard_app, "_etsy_update_shop_is_busy", return_value=False
        ), patch.object(
            dashboard_app, "_run_etsy_updater", side_effect=no_op_sync
        ), patch.object(
            dashboard_app, "_get_job_store", fake_store_factory
        ), patch.object(
            dashboard_app, "_DASHBOARD_MUTATION_TOKEN", self._token
        ):
            post_response = self._client.post(
                "/api/products/4/push-to-etsy",
                headers=self._headers(),
                json=payload,
            )
            self.assertEqual(200, post_response.status_code)
            post_payload = post_response.json()
            self.assertEqual(True, post_payload.get("ok"))
            job_id = post_payload.get("job_id")
            self.assertIsInstance(job_id, str)

            status_response = self._client.get(
                "/api/etsy/update-status",
                params={"job_id": job_id},
            )
            self.assertEqual(200, status_response.status_code)
            status_payload = status_response.json()
            self.assertEqual(True, status_payload.get("ok"))
            self.assertEqual(job_id, status_payload.get("job_id"))
            self.assertEqual("product-01", status_payload.get("folder"))
            self.assertEqual("starting", status_payload.get("status"))

            store_record = self._store.get_job(job_id)
            self.assertIsNotNone(store_record)
            self.assertEqual(store_record["operation"], "etsy_push_update")
            self.assertEqual(store_record["shop_id"], "templystudios")
            self.assertEqual(store_record["listing_id"], "1234567890")



class DashboardSyncAsyncIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_local_entry_point_does_not_recheck_active_shop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="etsy-dashboard-sync-int-") as workspace:
            product_path = Path(workspace) / "product-01"
            product_path.mkdir(parents=True, exist_ok=True)
            excel_path = Path(workspace) / "Etsy_SEO_Generator.xlsx"

            with patch.object(
                dashboard_app,
                "_assert_shop_identity",
                new=MagicMock(side_effect=RuntimeError("should not run")),
            ) as assert_shop_identity, patch.object(
                dashboard_app,
                "scrape_listing_details",
                AsyncMock(
                    return_value={
                        "ok": True,
                        "title": "X",
                        "description": "Y",
                        "tags": "z",
                    },
                ),
            ), patch.object(
                dashboard_app, "save_to_excel"
            ):
                details, _assets, status, sync_ok, synced_fields = await dashboard_app._sync_local_from_etsy(
                    listing_id="9999",
                    row=4,
                    shop_id="templystudios",
                    product_path=product_path,
                    excel_path=excel_path,
                    sync_assets=False,
                )
                self.assertEqual(details["title"], "X")
                self.assertTrue(sync_ok)
                self.assertEqual(synced_fields, ["description", "tags", "title"])
                self.assertIn("metadata_ok", status)
                assert_shop_identity.assert_not_called()


if __name__ == "__main__":
    unittest.main()
