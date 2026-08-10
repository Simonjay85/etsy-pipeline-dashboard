#!/usr/bin/env python3
"""Regression coverage for the durable stop-all response and cancellation path."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard_app
from job_store import JOB_STATUS_CANCELLED, JOB_STATUS_QUEUED, JOB_STATUS_RUNNING, JobStore


class DashboardStopAllRegressionTests(TestCase):
    def test_route_returns_json_and_cancels_durable_queued_and_running_jobs(self) -> None:
        with TemporaryDirectory(prefix="etsy-stop-all-regression-") as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs.sqlite")
            try:
                queued, _ = store.create_or_get_deduplicated_job(
                    shop_id="templystudios",
                    operation="etsy_push_update",
                    row=4,
                    folder="product-queued",
                    listing_id="",
                    request_id="stop-all-queued",
                    dedupe_key="stop-all:product-queued",
                    operation_receipt={"request_id": "stop-all-queued"},
                )
                running, _ = store.create_or_get_deduplicated_job(
                    shop_id="templystudios",
                    operation="etsy_push_update",
                    row=5,
                    folder="product-running",
                    listing_id="",
                    request_id="stop-all-running",
                    dedupe_key="stop-all:product-running",
                    operation_receipt={"request_id": "stop-all-running"},
                )
                store.mark_running(running["job_id"], pid=12345)

                dashboard_app._running_processes.clear()
                dashboard_app._running_tasks.clear()
                client = TestClient(dashboard_app.app, base_url="http://127.0.0.1:8090")
                headers = {
                    dashboard_app._DASHBOARD_MUTATION_TOKEN_HEADER: dashboard_app._DASHBOARD_MUTATION_TOKEN,
                    "Origin": "http://127.0.0.1:8090",
                }

                with patch.object(dashboard_app, "_get_job_store", return_value=store):
                    response = client.post("/api/stop-all", headers=headers)

                self.assertEqual(200, response.status_code)
                self.assertTrue(response.headers["content-type"].startswith("application/json"))
                payload = response.json()
                self.assertEqual({"ok": True, "stopped": [], "cancelled": []}, payload)

                queued_after = store.get_job(queued["job_id"])
                running_after = store.get_job(running["job_id"])
                self.assertIsNotNone(queued_after)
                self.assertIsNotNone(running_after)
                self.assertEqual(JOB_STATUS_CANCELLED, queued_after["status"])
                self.assertEqual(JOB_STATUS_CANCELLED, running_after["status"])
                self.assertEqual([], store.list_jobs(status={JOB_STATUS_QUEUED, JOB_STATUS_RUNNING}))
            finally:
                store.close()
                dashboard_app._running_processes.clear()
                dashboard_app._running_tasks.clear()


if __name__ == "__main__":
    import unittest

    unittest.main()
