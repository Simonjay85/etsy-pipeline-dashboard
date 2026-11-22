from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from job_store import (
    PIPELINE_STATUS_QUEUED,
    PIPELINE_STATUS_UNKNOWN,
    PIPELINE_STATUS_VERIFIED,
    JobStoreError,
    JobStore,
)
from pipeline_contracts import PipelinePolicy


class PipelineStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="etsy-pipeline-store-")
        self.store = JobStore(Path(self.tmp.name) / "jobs.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _create(self, **overrides):
        args = {
            "campaign_id": "etsy-20260912",
            "shop_id": "templystudios",
            "stage": "research",
            "candidate_id": "candidate-1",
            "input_version": "v1",
            "source_fingerprint": "abc",
            "idempotency_key": "idem-1",
            "payload": {"listing_type": "digital"},
        }
        args.update(overrides)
        return self.store.create_pipeline_job(**args)

    def _bind_manifest(self, job, claimed, receipt, *, source_job_id=None):
        customer_hash = receipt["customer_file_hashes"][0]
        image_hashes = receipt.get("image_hashes") or [hashlib.sha256(f"synthetic-image-{index}".encode()).hexdigest() for index in range(10)]
        manifest = {
            "schema": "etsy.pipeline-product.v1",
            "contract_version": job["contract_version"],
            "pipeline_job_id": job["job_id"],
            "campaign_id": job["campaign_id"],
            "candidate_id": job["candidate_id"],
            "product_id": job["product_id"],
            "shop_id": job["shop_id"],
            "listing_type": "digital",
            "customer_files": ["buyer.zip"],
            "customer_file_evidence": [{"path": "buyer.zip", "sha256": customer_hash, "size": 1}],
            "image_files": [f"images/image-{index}.png" for index in range(10)],
            "image_file_evidence": [
                {"path": f"images/image-{index}.png", "sha256": image_hashes[index], "size": 1}
                for index in range(10)
            ],
            "source_assets": [],
            "source_provenance": [],
        }
        return self.store.record_pipeline_manifest(
            job["job_id"],
            worker_id=claimed["lease_owner"],
            fence=claimed["fence"],
            manifest_sha256=receipt["manifest_sha256"],
            manifest=manifest,
            source_job_id=source_job_id,
        )

    def test_candidate_exists_before_product_and_idempotency_is_strict(self):
        first, created = self._create()
        self.assertTrue(created)
        self.assertEqual(first["product_id"], "")
        again, created_again = self._create(payload={"listing_type": "digital", "changed": True})
        self.assertFalse(created_again)
        self.assertEqual(first["job_id"], again["job_id"])

    def test_lease_fence_rejects_stale_worker_and_allows_heartbeat(self):
        job, _ = self._create()
        claimed = self.store.claim_pipeline_job(job["job_id"], worker_id="hermes-research")
        self.assertIsNotNone(claimed)
        fence = claimed["fence"]
        self.assertTrue(self.store.heartbeat_pipeline_job(job["job_id"], worker_id="hermes-research", fence=fence))
        self.assertFalse(self.store.heartbeat_pipeline_job(job["job_id"], worker_id="old-worker", fence=fence))
        self.assertFalse(self.store.transition_pipeline_job(job["job_id"], "waiting", worker_id="old-worker", fence=fence))
        self.assertTrue(self.store.transition_pipeline_job(job["job_id"], "waiting", worker_id="hermes-research", fence=fence))

    def test_restart_at_etsy_boundary_becomes_unknown_and_is_not_claimable(self):
        job, _ = self._create(
            stage="create-verified-draft",
            product_id="product-1",
            side_effect_boundary="etsy_write",
        )
        claimed = self.store.claim_pipeline_job(job["job_id"], worker_id="poster")
        self.assertIsNotNone(claimed)
        self.store._conn.execute("UPDATE pipeline_jobs SET lease_expires_at = 0 WHERE id = ?", (job["job_id"],))
        self.store._conn.commit()
        recovered = self.store.recover_pipeline_jobs()
        self.assertEqual(recovered["unknown"], 1)
        current = self.store.get_pipeline_job(job["job_id"])
        self.assertEqual(current["status"], PIPELINE_STATUS_UNKNOWN)
        self.assertIsNone(self.store.claim_pipeline_job(job["job_id"], worker_id="poster-retry"))

    def test_quota_is_atomic_and_verified_consumes_slot(self):
        job, _ = self._create(stage="create-verified-draft", product_id="product-1", side_effect_boundary="etsy_write")
        claimed = self.store.claim_pipeline_job(job["job_id"], worker_id="poster")
        fence = claimed["fence"]
        quota_day = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()
        reservation = self.store.reserve_pipeline_quota(job["job_id"], shop_id="templystudios", quota_date=quota_day, worker_id="poster", fence=fence)
        self.assertTrue(reservation["reserved"])
        self.assertTrue(self.store.reserve_pipeline_quota(job["job_id"], shop_id="templystudios", quota_date=quota_day, worker_id="poster", fence=fence)["reserved"])
        hashes = [hashlib.sha256(f"image-{i}".encode()).hexdigest() for i in range(10)]
        receipt = {
            "job_id": job["job_id"], "campaign_id": job["campaign_id"], "candidate_id": job["candidate_id"], "shop_id": job["shop_id"], "product_id": job["product_id"],
            "worker_id": "poster", "fence": fence, "listing_id": "123", "listing_status": "draft",
            "readback_url": "https://www.etsy.com/listing/123", "readback_ok": True,
            "image_hashes": hashes, "customer_file_hashes": [hashlib.sha256(b"buyer").hexdigest()],
            "manifest_sha256": hashlib.sha256(b"manifest").hexdigest(), "asset_qa_ok": True,
            "seo_validated": True, "customer_files_validated": True,
        }
        self._bind_manifest(job, claimed, receipt)
        completed = self.store.complete_verified(job["job_id"], worker_id="poster", fence=fence, receipt=receipt)
        self.assertEqual(PIPELINE_STATUS_VERIFIED, completed["job"]["status"])
        replay = self.store.complete_verified(job["job_id"], worker_id="poster", fence=fence, receipt=receipt)
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.store.pipeline_quota_snapshot(quota_date=quota_day)["used"], 1)

    def test_verified_requires_draft_stage_and_active_fence(self):
        job, _ = self._create()
        with self.assertRaises(Exception):
            self.store.transition_pipeline_job(job["job_id"], PIPELINE_STATUS_VERIFIED, receipt={})
        claimed = self.store.claim_pipeline_job(job["job_id"], worker_id="worker")
        self.assertFalse(self.store.transition_pipeline_job(job["job_id"], "waiting", worker_id="worker", fence=claimed["fence"] + 1))

    def test_verified_receipt_hashes_must_match_accepted_manifest_evidence(self):
        job, _ = self._create(
            idempotency_key="manifest-evidence-mismatch",
            stage="create-verified-draft",
            product_id="product-evidence",
            side_effect_boundary="etsy_write",
        )
        claimed = self.store.claim_pipeline_job(job["job_id"], worker_id="poster")
        assert claimed is not None
        fence = claimed["fence"]
        reservation = self.store.reserve_pipeline_quota(
            job["job_id"],
            shop_id="templystudios",
            worker_id="poster",
            fence=fence,
        )
        self.assertTrue(reservation["reserved"])
        expected_customer = hashlib.sha256(b"expected-buyer").hexdigest()
        expected_manifest = hashlib.sha256(b"expected-manifest").hexdigest()
        self._bind_manifest(
            job,
            claimed,
            {
                "customer_file_hashes": [expected_customer],
                "manifest_sha256": expected_manifest,
                "image_hashes": [hashlib.sha256(f"expected-image-{index}".encode()).hexdigest() for index in range(10)],
            },
        )
        receipt = {
            "job_id": job["job_id"],
            "campaign_id": job["campaign_id"],
            "candidate_id": job["candidate_id"],
            "shop_id": job["shop_id"],
            "product_id": job["product_id"],
            "worker_id": "poster",
            "fence": fence,
            "listing_id": "789",
            "listing_status": "draft",
            "readback_url": "https://www.etsy.com/listing/789",
            "readback_ok": True,
            "image_hashes": [hashlib.sha256(f"evidence-image-{i}".encode()).hexdigest() for i in range(10)],
            "customer_file_hashes": [hashlib.sha256(b"invented-buyer").hexdigest()],
            "manifest_sha256": hashlib.sha256(b"invented-manifest").hexdigest(),
            "asset_qa_ok": True,
            "seo_validated": True,
            "customer_files_validated": True,
        }
        image_only_mismatch = dict(receipt)
        image_only_mismatch["manifest_sha256"] = expected_manifest
        image_only_mismatch["customer_file_hashes"] = [expected_customer]
        image_only_mismatch["image_hashes"] = [
            hashlib.sha256(f"expected-image-{index}".encode()).hexdigest()
            for index in range(10)
        ]
        image_only_mismatch["image_hashes"][0] = hashlib.sha256(b"different-image").hexdigest()
        with self.assertRaises(JobStoreError) as image_error:
            self.store.complete_verified(job["job_id"], worker_id="poster", fence=fence, receipt=image_only_mismatch)
        self.assertIn("image hashes", str(image_error.exception))
        with self.assertRaises(JobStoreError) as error:
            self.store.complete_verified(job["job_id"], worker_id="poster", fence=fence, receipt=receipt)
        self.assertIn("does not match accepted manifest", str(error.exception))
        self.assertEqual("running", self.store.get_pipeline_job(job["job_id"])["status"])

    def test_verified_rolls_held_quota_forward_after_midnight(self):
        job, _ = self._create(
            idempotency_key="quota-rollover",
            stage="create-verified-draft",
            product_id="product-rollover",
            side_effect_boundary="etsy_write",
        )
        claimed = self.store.claim_pipeline_job(job["job_id"], worker_id="poster")
        assert claimed is not None
        fence = claimed["fence"]
        receipt = {
            "job_id": job["job_id"],
            "campaign_id": job["campaign_id"],
            "candidate_id": job["candidate_id"],
            "shop_id": job["shop_id"],
            "product_id": job["product_id"],
            "worker_id": "poster",
            "fence": fence,
            "listing_id": "456",
            "listing_status": "draft",
            "readback_url": "https://www.etsy.com/listing/456",
            "readback_ok": True,
            "image_hashes": [hashlib.sha256(f"rollover-image-{i}".encode()).hexdigest() for i in range(10)],
            "customer_file_hashes": [hashlib.sha256(b"rollover-buyer").hexdigest()],
            "manifest_sha256": hashlib.sha256(b"rollover-manifest").hexdigest(),
            "asset_qa_ok": True,
            "seo_validated": True,
            "customer_files_validated": True,
        }
        # The first call is the reservation day; the second is completion day.
        with patch("job_store._pipeline_quota_date", side_effect=["2026-09-12", "2026-09-13"]):
            reserved = self.store.reserve_pipeline_quota(
                job["job_id"],
                shop_id="templystudios",
                quota_date="2026-09-12",
                worker_id="poster",
                fence=fence,
            )
            self.assertTrue(reserved["reserved"])
            self._bind_manifest(job, claimed, receipt)
            completed = self.store.complete_verified(
                job["job_id"],
                worker_id="poster",
                fence=fence,
                receipt=receipt,
            )
        self.assertEqual("verified", completed["job"]["status"])
        self.assertEqual("2026-09-13", self.store.get_pipeline_quota_reservation(job["job_id"])["quota_date"])
        self.assertEqual(1, self.store.pipeline_quota_snapshot(quota_date="2026-09-13")["used"])

    def test_prepare_pipeline_write_rolls_quota_before_external_save(self):
        job, _ = self._create(
            idempotency_key="quota-prepare-rollover",
            stage="create-verified-draft",
            product_id="product-prepare-rollover",
            side_effect_boundary="etsy_write",
        )
        claimed = self.store.claim_pipeline_job(job["job_id"], worker_id="poster")
        assert claimed is not None
        with patch("job_store._pipeline_quota_date", side_effect=["2026-09-12", "2026-09-13"]):
            reserved = self.store.reserve_pipeline_quota(
                job["job_id"],
                shop_id="templystudios",
                quota_date="2026-09-12",
                worker_id="poster",
                fence=claimed["fence"],
            )
            self.assertTrue(reserved["reserved"])
            ready = self.store.prepare_pipeline_write(
                job["job_id"],
                worker_id="poster",
                fence=claimed["fence"],
            )
        self.assertEqual("write_ready", ready["reason"])
        self.assertEqual("2026-09-13", ready["quota_date"])
        self.assertEqual("held", self.store.get_pipeline_quota_reservation(job["job_id"])["status"])

    def test_verified_rejects_midnight_rollover_when_current_quota_is_full(self):
        policy = PipelinePolicy(max_drafts_per_day=1, max_drafts_per_shop_per_day=1)
        first, _ = self._create(
            idempotency_key="quota-full-current",
            stage="create-verified-draft",
            product_id="product-current",
            side_effect_boundary="etsy_write",
        )
        first_claimed = self.store.claim_pipeline_job(first["job_id"], worker_id="poster-first")
        assert first_claimed is not None
        first_receipt = {
            "job_id": first["job_id"],
            "campaign_id": first["campaign_id"],
            "candidate_id": first["candidate_id"],
            "shop_id": first["shop_id"],
            "product_id": first["product_id"],
            "worker_id": "poster-first",
            "fence": first_claimed["fence"],
            "listing_id": "457",
            "listing_status": "draft",
            "readback_url": "https://www.etsy.com/listing/457",
            "readback_ok": True,
            "image_hashes": [hashlib.sha256(f"full-image-{i}".encode()).hexdigest() for i in range(10)],
            "customer_file_hashes": [hashlib.sha256(b"full-buyer").hexdigest()],
            "manifest_sha256": hashlib.sha256(b"full-manifest").hexdigest(),
            "asset_qa_ok": True,
            "seo_validated": True,
            "customer_files_validated": True,
        }
        with patch("job_store._pipeline_quota_date", return_value="2026-09-13"):
            first_reserved = self.store.reserve_pipeline_quota(
                first["job_id"],
                shop_id="templystudios",
                worker_id="poster-first",
                fence=first_claimed["fence"],
                policy=policy,
            )
            self.assertTrue(first_reserved["reserved"])
            self._bind_manifest(first, first_claimed, first_receipt)
            self.store.complete_verified(
                first["job_id"],
                worker_id="poster-first",
                fence=first_claimed["fence"],
                receipt=first_receipt,
                policy=policy,
            )

        second, _ = self._create(
            idempotency_key="quota-rollover-full",
            stage="create-verified-draft",
            product_id="product-rollover-full",
            side_effect_boundary="etsy_write",
        )
        second_claimed = self.store.claim_pipeline_job(second["job_id"], worker_id="poster-second")
        assert second_claimed is not None
        with patch("job_store._pipeline_quota_date", side_effect=["2026-09-12", "2026-09-13"]):
            second_reserved = self.store.reserve_pipeline_quota(
                second["job_id"],
                shop_id="templystudios",
                quota_date="2026-09-12",
                worker_id="poster-second",
                fence=second_claimed["fence"],
                policy=policy,
            )
            self.assertTrue(second_reserved["reserved"])
            second_receipt = dict(first_receipt)
            second_receipt.update(
                {
                    "job_id": second["job_id"],
                    "shop_id": second["shop_id"],
                    "product_id": second["product_id"],
                    "worker_id": "poster-second",
                    "fence": second_claimed["fence"],
                    "listing_id": "458",
                    "readback_url": "https://www.etsy.com/listing/458",
                }
            )
            self._bind_manifest(second, second_claimed, second_receipt)
            with self.assertRaises(JobStoreError) as error:
                self.store.complete_verified(
                    second["job_id"],
                    worker_id="poster-second",
                    fence=second_claimed["fence"],
                    receipt=second_receipt,
                    policy=policy,
                )
            self.assertIn("current-day daily quota exhausted", str(error.exception))
        self.assertEqual("running", self.store.get_pipeline_job(second["job_id"])["status"])
        self.assertEqual("2026-09-12", self.store.get_pipeline_quota_reservation(second["job_id"])["quota_date"])

    def test_expired_lease_cannot_heartbeat_or_transition(self):
        job, _ = self._create()
        claimed = self.store.claim_pipeline_job(job["job_id"], worker_id="worker")
        self.store._conn.execute("UPDATE pipeline_jobs SET lease_expires_at = 0 WHERE id = ?", (job["job_id"],))
        self.store._conn.commit()
        self.assertFalse(self.store.heartbeat_pipeline_job(job["job_id"], worker_id="worker", fence=claimed["fence"]))
        self.assertFalse(self.store.transition_pipeline_job(job["job_id"], "waiting", worker_id="worker", fence=claimed["fence"]))

    def test_image_and_etsy_write_claims_are_serialized(self):
        image_one, _ = self._create(stage="generate-images", idempotency_key="image-one")
        image_two, _ = self._create(stage="generate-images", idempotency_key="image-two")
        self.assertIsNotNone(self.store.claim_pipeline_job(image_one["job_id"], worker_id="image-worker"))
        self.assertIsNone(self.store.claim_pipeline_job(image_two["job_id"], worker_id="image-worker-2"))
        draft_one, _ = self._create(stage="create-verified-draft", product_id="draft-one", side_effect_boundary="etsy_write", idempotency_key="draft-one")
        draft_two, _ = self._create(stage="create-verified-draft", product_id="draft-two", side_effect_boundary="etsy_write", idempotency_key="draft-two")
        self.assertIsNotNone(self.store.claim_pipeline_job(draft_one["job_id"], worker_id="poster-one"))
        self.assertIsNone(self.store.claim_pipeline_job(draft_two["job_id"], worker_id="poster-two"))

    def test_outbox_is_idempotent_and_retryable(self):
        job, _ = self._create()
        first = self.store.enqueue_pipeline_event(job_id=job["job_id"], event_type="created", idempotency_key="event-1", payload={"x": 1})
        second = self.store.enqueue_pipeline_event(job_id=job["job_id"], event_type="created", idempotency_key="event-1", payload={"x": 2})
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertTrue(self.store.mark_pipeline_event_delivered(first["event_id"], error="offline"))
        self.assertEqual(len(self.store.list_pipeline_outbox()), 1)
        self.assertTrue(self.store.mark_pipeline_event_delivered(first["event_id"]))
        self.assertEqual(self.store.list_pipeline_outbox(), [])


if __name__ == "__main__":
    unittest.main()
