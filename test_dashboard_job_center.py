from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import dashboard_app
from job_store import JOB_STATUS_SUCCEEDED, JobStore


class DashboardJobCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="etsy-job-center-")
        self.store = JobStore(Path(self.temp_dir.name) / "jobs.sqlite")
        self.client = TestClient(dashboard_app.app, base_url="http://127.0.0.1:8090")
        self.token = patch.object(dashboard_app, "_DASHBOARD_MUTATION_TOKEN", "job-center-token")
        self.token.start()
        dashboard_app._running_tasks.clear()
        dashboard_app._running_processes.clear()

    def tearDown(self) -> None:
        dashboard_app._running_tasks.clear()
        dashboard_app._running_processes.clear()
        self.store.close()
        self.token.stop()
        self.temp_dir.cleanup()

    def _headers(self) -> dict[str, str]:
        return {
            "Host": "127.0.0.1:8090",
            "Origin": "http://127.0.0.1:8090",
            dashboard_app._DASHBOARD_MUTATION_TOKEN_HEADER: "job-center-token",
        }

    def _create(self, shop_id: str, suffix: str, *, fields: list[str] | None = None) -> dict:
        job, _ = self.store.create_or_get_deduplicated_job(
            shop_id=shop_id,
            operation="etsy_push_update",
            row=4,
            folder=f"product-{suffix}",
            listing_id=f"123456{suffix}",
            request_id=f"request-{suffix}",
            dedupe_key=f"{shop_id}:etsy_push_update:product-{suffix}",
            operation_receipt={"fields": fields or ["title"]},
            fields=fields or ["title"],
        )
        return job

    def test_list_is_scoped_and_safe(self) -> None:
        self._create("templystudios", "04")
        self._create("daisyflowdigital", "05")
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), patch.object(
            dashboard_app, "_get_job_store", return_value=self.store
        ):
            response = self.client.get("/api/etsy/jobs?limit=20")
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertEqual(["templystudios"], sorted({job["shop_id"] for job in payload["jobs"]}))
        self.assertNotIn("operation_receipt", payload["jobs"][0])
        self.assertNotIn("/Users/", response.text)

    def test_cancel_one_does_not_cancel_other_shop(self) -> None:
        job = self._create("templystudios", "04")
        other = self._create("daisyflowdigital", "05")
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), patch.object(
            dashboard_app, "_get_job_store", return_value=self.store
        ):
            response = self.client.post(f"/api/etsy/jobs/{job['job_id']}/cancel", headers=self._headers())
            wrong_shop = self.client.post(f"/api/etsy/jobs/{other['job_id']}/cancel", headers=self._headers())
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("cancelled", self.store.get_job(job["job_id"])["status"])
        self.assertEqual(409, wrong_shop.status_code)
        self.assertEqual("queued", self.store.get_job(other["job_id"])["status"])

    def test_retry_creates_parent_lineage_and_rejects_succeeded(self) -> None:
        failed = self._create("templystudios", "04", fields=["title"])
        self.store.mark_failed(failed["job_id"], log_excerpt="safe failure")

        def fake_create_task(coroutine):
            coroutine.close()
            return object()

        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), patch.object(
            dashboard_app, "_get_job_store", return_value=self.store
        ), patch.object(dashboard_app.asyncio, "create_task", side_effect=fake_create_task), patch.object(
            dashboard_app, "_register_background_task", Mock()
        ):
            response = self.client.post(f"/api/etsy/jobs/{failed['job_id']}/retry", headers=self._headers())

        self.assertEqual(200, response.status_code, response.text)
        retried = self.store.get_job(response.json()["job_id"])
        self.assertEqual(failed["job_id"], retried["retry_parent_job_id"])
        self.assertEqual(["title"], retried["fields"])

        succeeded = self._create("templystudios", "06")
        self.store.mark_running(succeeded["job_id"], pid=1)
        self.store.mark_succeeded(succeeded["job_id"])
        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), patch.object(
            dashboard_app, "_get_job_store", return_value=self.store
        ):
            rejected = self.client.post(f"/api/etsy/jobs/{succeeded['job_id']}/retry", headers=self._headers())
        self.assertEqual(409, rejected.status_code)
        self.assertEqual(JOB_STATUS_SUCCEEDED, self.store.get_job(succeeded["job_id"])["status"])

    def test_same_folder_in_different_shops_has_independent_runtime_identity(self) -> None:
        first = self._create("templystudios", "01")
        second = self._create("daisyflowdigital", "01")
        self.store.mark_running(first["job_id"], pid=101)
        self.store.mark_running(second["job_id"], pid=202)
        dashboard_app._running_processes[first["job_id"]] = Mock()
        dashboard_app._running_processes[second["job_id"]] = Mock()

        with patch.object(dashboard_app, "_active_shop_id", "templystudios"), patch.object(
            dashboard_app, "_get_job_store", return_value=self.store
        ):
            response = self.client.post(f"/api/etsy/jobs/{first['job_id']}/cancel", headers=self._headers())

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("cancelled", self.store.get_job(first["job_id"])["status"])
        self.assertEqual("running", self.store.get_job(second["job_id"])["status"])
        self.assertIn(second["job_id"], dashboard_app._running_processes)


if __name__ == "__main__":
    unittest.main()
