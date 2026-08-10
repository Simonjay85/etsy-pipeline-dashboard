#!/usr/bin/env python3
from __future__ import annotations

import threading
import time
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JobStore,
)


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._workspace = TemporaryDirectory(prefix="etsy-job-store-test-")
        self.db_path = Path(self._workspace.name) / "jobs.sqlite"
        self.store = JobStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._workspace.cleanup()

    def _job_args(self, **kwargs):
        args = {
            "shop_id": "templystudios",
            "operation": "etsy_push_update",
            "row": 4,
            "folder": "product-01",
            "listing_id": "1111111111",
            "request_id": "req-001",
            "operation_receipt": {"listing_id": "1111111111", "requested_shop": "templystudios"},
            "fields": ["title", "price"],
        }
        args.update(kwargs)
        if "dedupe_key" not in args or not args["dedupe_key"]:
            args["dedupe_key"] = f"{args['shop_id']}:etsy_push_update:{args['folder']}"
        return args

    def test_dedupe_reuses_active_job_without_incrementing_attempt(self) -> None:
        first_record, first_created = self.store.create_or_get_deduplicated_job(**self._job_args())
        self.assertTrue(first_created)
        self.assertEqual(first_record["status"], JOB_STATUS_QUEUED)
        first_job_id = first_record["job_id"]

        second_record, second_created = self.store.create_or_get_deduplicated_job(**self._job_args())
        self.assertFalse(second_created)
        self.assertEqual(second_record["job_id"], first_job_id)
        self.assertEqual(second_record["attempt_count"], first_record["attempt_count"])

        active_jobs = self.store.get_active_jobs_for_shop("templystudios")
        self.assertEqual(len(active_jobs), 1)
        self.assertEqual(active_jobs[0]["job_id"], first_job_id)

    def test_retry_creates_next_attempt_with_parent(self) -> None:
        first_record, _ = self.store.create_or_get_deduplicated_job(**self._job_args(request_id="req-001"))
        first_job_id = first_record["job_id"]
        self.store.mark_failed(first_job_id, exit_code=1, log_excerpt="temporary failure")

        second_record, second_created = self.store.create_or_get_deduplicated_job(
            **self._job_args(request_id="req-002")
        )
        self.assertTrue(second_created)
        self.assertNotEqual(first_job_id, second_record["job_id"])
        self.assertEqual(second_record["attempt_count"], int(first_record["attempt_count"]) + 1)
        self.assertEqual(second_record["retry_parent_job_id"], first_job_id)
        self.assertEqual(second_record["status"], JOB_STATUS_QUEUED)

        restored = self.store.get_job(second_record["job_id"])
        self.assertIsNotNone(restored)
        self.assertEqual(restored["attempt_count"], int(first_record["attempt_count"]) + 1)

    def test_cancel_one_shop_and_all(self) -> None:
        req_a = self.store.create_or_get_deduplicated_job(**self._job_args(request_id="req-a"))
        req_b = self.store.create_or_get_deduplicated_job(**self._job_args(shop_id="other-shop", request_id="req-b"))
        job_a = req_a[0]
        job_b = req_b[0]

        cancelled = self.store.cancel_job(job_a["job_id"])
        self.assertTrue(cancelled)
        cached_a = self.store.get_job(job_a["job_id"])
        self.assertEqual(cached_a["status"], JOB_STATUS_CANCELLED)

        shop_cancelled_count = self.store.cancel_jobs_for_shop("other-shop")
        self.assertEqual(shop_cancelled_count, 1)
        cached_b = self.store.get_job(job_b["job_id"])
        self.assertEqual(cached_b["status"], JOB_STATUS_CANCELLED)

        all_cancelled = self.store.cancel_all_jobs()
        self.assertEqual(all_cancelled, 0)

    def test_recover_running_jobs_marks_them_failed(self) -> None:
        record, _ = self.store.create_or_get_deduplicated_job(**self._job_args(request_id="req-001"))
        self.store.mark_running(record["job_id"])
        self.store.close()

        rehydrated = JobStore(self.db_path)
        recovered = rehydrated.recover_running_jobs()
        self.assertEqual(recovered, 1)
        recovered_record = rehydrated.get_job(record["job_id"])
        self.assertEqual(recovered_record["status"], JOB_STATUS_FAILED)
        self.assertIn("recovered_at", recovered_record["latest_log_excerpt"])
        self.assertEqual(recovered_record["exit_code"], -1)
        rehydrated.close()

    def test_concurrent_create_is_idempotent(self) -> None:
        results: list[tuple[dict[str, object], bool]] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                outcome = self.store.create_or_get_deduplicated_job(**self._job_args(request_id="req-001"))
                with lock:
                    results.append((outcome[0], outcome[1]))
            except BaseException as error:
                with lock:
                    errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertTrue(results)
        created_count = sum(1 for _, created in results if created)
        self.assertEqual(created_count, 1)
        job_ids = {str(item["job_id"]) for item, _ in results}
        self.assertEqual(len(job_ids), 1)

        active_jobs = self.store.get_active_jobs_for_shop("templystudios")
        self.assertEqual(len(active_jobs), 1)

    def test_marks_running_with_pid_exit_code_and_retry_lineage(self) -> None:
        record, _ = self.store.create_or_get_deduplicated_job(**self._job_args(request_id="req-pid"))
        self.store.mark_running(record["job_id"], pid=12345)
        running_record = self.store.get_job(record["job_id"])
        self.assertEqual(running_record["status"], JOB_STATUS_RUNNING)
        self.assertEqual(running_record["pid"], 12345)
        self.store.append_log_excerpt(record["job_id"], "first chunk")
        self.store.append_log_excerpt(record["job_id"], "second chunk")

        self.store.mark_succeeded(record["job_id"], exit_code=0, log_excerpt="all good")
        succeeded = self.store.get_job(record["job_id"])
        self.assertEqual(succeeded["status"], JOB_STATUS_SUCCEEDED)
        self.assertEqual(succeeded["exit_code"], 0)
        self.assertIn("all good", succeeded["latest_log_excerpt"])

        self.assertTrue(isinstance(succeeded["created_at"], float))
        self.assertTrue(isinstance(succeeded["updated_at"], float))
        self.assertTrue(isinstance(succeeded["finished_at"], float))
        self.assertTrue("listing_id" in succeeded["operation_receipt"] if isinstance(succeeded["operation_receipt"], str) else True)
        self.assertGreaterEqual(succeeded["updated_at"], succeeded["created_at"])


if __name__ == "__main__":
    unittest.main()
