#!/usr/bin/env python3
"""Narrow coverage for push-update route integration with persistent job store."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import dashboard_app
from job_store import JOB_STATUS_QUEUED, JobStore


class _JsonRequest:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = dict(payload)

    async def json(self) -> dict[str, object]:
        return self._payload


class TestEtsyUpdateJobsPersistence(IsolatedAsyncioTestCase):
    """Verify update-route dedupe and status read-back stay on durable store."""

    def setUp(self) -> None:
        self.product = {
            "folder": "product-01",
            "etsy_url": "https://www.etsy.com/listing/1234567890",
        }
        self.request_payload = {
            "shop": "templystudios",
            "folder": "product-01",
            "listing_id": "1234567890",
            "fields": ["title"],
        }
        dashboard_app._etsy_update_jobs.clear()

    async def test_push_route_uses_store_dedupe_and_reuses_job(self) -> None:
        dashboard_app._running_processes.clear()
        dashboard_app._running_tasks.clear()
        with TemporaryDirectory(prefix="etsy-update-job-store-") as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs.sqlite")
            enqueue_operation = AsyncMock(
                return_value=(
                    {"command_id": "test-etsy-update-command", "status": JOB_STATUS_QUEUED},
                    True,
                )
            )
            with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
                patch.object(
                    dashboard_app,
                    "get_product_by_row",
                    return_value=self.product,
                ), \
                patch.object(dashboard_app, "_enqueue_operation", enqueue_operation), \
                patch.object(dashboard_app, "_get_job_store", return_value=store):

                response = await dashboard_app.push_local_updates_to_etsy(4, _JsonRequest(self.request_payload))
                self.assertTrue(response["ok"])
                job_id = str(response["job_id"])
                self.assertIsNotNone(job_id)
                persisted = store.get_job(job_id)
                self.assertIsNotNone(persisted)
                self.assertEqual(persisted["shop_id"], "templystudios")
                self.assertEqual(persisted["status"], JOB_STATUS_QUEUED)
                self.assertEqual(persisted["folder"], "product-01")

                dashboard_app._running_tasks.clear()
                second = await dashboard_app.push_local_updates_to_etsy(4, _JsonRequest(self.request_payload))
                self.assertTrue(second["ok"])
                self.assertFalse(second["created"])
                self.assertEqual(job_id, second["job_id"])
                self.assertIn("đã có trong hàng chờ", second["message"])
                enqueue_operation.assert_awaited_once()

    async def test_etsy_update_status_reads_store_payload(self) -> None:
        dashboard_app._running_processes.clear()
        dashboard_app._running_tasks.clear()
        with TemporaryDirectory(prefix="etsy-update-job-store-") as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs.sqlite")
            enqueue_operation = AsyncMock(
                return_value=(
                    {"command_id": "test-etsy-update-command", "status": JOB_STATUS_QUEUED},
                    True,
                )
            )
            with patch.object(dashboard_app, "_active_shop_id", "templystudios"), \
                patch.object(dashboard_app, "get_product_by_row", return_value=self.product), \
                patch.object(dashboard_app, "_enqueue_operation", enqueue_operation), \
                patch.object(dashboard_app, "_get_job_store", return_value=store):

                response = await dashboard_app.push_local_updates_to_etsy(4, _JsonRequest(self.request_payload))
                job_id = str(response["job_id"])
                status = await dashboard_app.etsy_update_status(job_id)

                self.assertTrue(status["ok"])
                self.assertEqual(status["job_id"], job_id)
                self.assertEqual(status["operation"], "etsy_push_update")
                self.assertIn(status["status"], {"starting", JOB_STATUS_QUEUED, "running"})
                enqueue_operation.assert_awaited_once()
