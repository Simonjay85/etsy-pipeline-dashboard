#!/usr/bin/env python3
"""Focused API coverage for the durable multi-shop Etsy pipeline.

The tests keep the dashboard boundary local: no Hermes dispatch, browser
session, Creative Fabrica download, ChatGPT generation, Etsy write, or Anna
network call is performed.  They exercise the contracts that protect those
side effects when a future Mac worker calls the API.
"""

from __future__ import annotations

import tempfile
import unittest
import hashlib
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import openpyxl
from fastapi.testclient import TestClient
from PIL import Image

import dashboard_app
import etsy_auto_post
from pipeline_artifacts import build_vector_delivery_zip


class DashboardPipelineApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="etsy-dashboard-pipeline-")
        self._root = Path(self._temporary.name)
        self._old_store = dashboard_app._ETSY_JOB_STORE
        dashboard_app._ETSY_JOB_STORE = None
        self._store_path_patch = patch.object(
            dashboard_app,
            "_JOB_STORE_PATH",
            self._root / ".etsy-dashboard-diagnostics" / "etsy_jobs.sqlite",
        )
        self._diagnostics_patch = patch.object(
            dashboard_app,
            "_DIAGNOSTICS_DIR",
            self._root / ".etsy-dashboard-diagnostics",
        )
        self._shops_patch = patch.object(
            dashboard_app,
            "SHOPS",
            {shop_id: {"id": shop_id, "name": shop_id} for shop_id in dashboard_app.PIPELINE_SHOP_IDS},
        )
        self._token_patch = patch.object(dashboard_app, "_DASHBOARD_MUTATION_TOKEN", "pipeline-test-token")
        self._anna_patch = patch.object(dashboard_app, "_ANNA_BRIDGE_TOKEN", "anna-bridge-test-token")
        self._store_path_patch.start()
        self._diagnostics_patch.start()
        self._shops_patch.start()
        self._token_patch.start()
        self._anna_patch.start()
        self.client = TestClient(dashboard_app.app, base_url="http://127.0.0.1:8090")

    def tearDown(self) -> None:
        store = dashboard_app._ETSY_JOB_STORE
        if store is not None and store is not self._old_store:
            store.close()
        dashboard_app._ETSY_JOB_STORE = self._old_store
        self._token_patch.stop()
        self._anna_patch.stop()
        self._shops_patch.stop()
        self._diagnostics_patch.stop()
        self._store_path_patch.stop()
        self._temporary.cleanup()

    def _headers(self) -> dict[str, str]:
        return {
            "Host": "127.0.0.1:8090",
            "Origin": "http://127.0.0.1:8090",
            dashboard_app._DASHBOARD_MUTATION_TOKEN_HEADER: "pipeline-test-token",
        }

    def _create(self, **overrides: object):
        payload: dict[str, object] = {
            "campaign_id": "camp-20260912",
            "shop_id": "templystudios",
            "candidate_id": "candidate-001",
            "product_id": "product-001",
            "stage": "research",
            "input_version": "brief-v1",
            "source_fingerprint": "source-sha-001",
            "idempotency_key": "pipeline-idem-001",
            "payload": {"brief": "weekly printable budget planner"},
        }
        payload.update(overrides)
        return self.client.post("/api/pipeline/jobs", headers=self._headers(), json=payload)

    def _bind_synthetic_manifest(
        self,
        job_id: str,
        worker_id: str,
        fence: int,
        manifest_hash: str,
        customer_hash: str,
        image_hashes: list[str] | None = None,
    ):
        store = dashboard_app._ETSY_JOB_STORE
        self.assertIsNotNone(store)
        assert store is not None
        job = store.get_pipeline_job(job_id)
        self.assertIsNotNone(job)
        assert job is not None
        image_hashes = image_hashes or [
            hashlib.sha256(f"synthetic-image-{index}".encode()).hexdigest()
            for index in range(10)
        ]
        return store.record_pipeline_manifest(
            job_id,
            worker_id=worker_id,
            fence=fence,
            manifest_sha256=manifest_hash,
            manifest={
                "schema": "etsy.pipeline-product.v1",
                "contract_version": job["contract_version"],
                "pipeline_job_id": job_id,
                "campaign_id": job["campaign_id"],
                "candidate_id": job["candidate_id"],
                "product_id": job["product_id"],
                "shop_id": job["shop_id"],
                "listing_type": "digital",
                "customer_files": ["buyer.zip"],
                "customer_file_evidence": [{"path": "buyer.zip", "sha256": customer_hash, "size": 1}],
                "image_files": [f"images/image-{index}.png" for index in range(10)],
                "image_file_evidence": [
                    {
                        "path": f"images/image-{index}.png",
                        "sha256": image_hashes[index],
                        "size": 1,
                    }
                    for index in range(10)
                ],
                "source_assets": [],
                "source_provenance": [],
            },
        )

    def test_health_exposes_policy_workers_and_all_four_digital_shops(self) -> None:
        response = self.client.get("/api/pipeline/health")

        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(5, payload["policy"]["max_drafts_per_day"])
        self.assertEqual(2, payload["policy"]["max_drafts_per_shop_per_day"])
        self.assertEqual(10, payload["policy"]["max_inflight_jobs"])
        self.assertEqual(0.0, payload["policy"]["api_budget_usd"])
        self.assertEqual(
            list(dashboard_app.PIPELINE_SHOP_IDS),
            [shop["id"] for shop in payload["shops"]],
        )
        self.assertTrue(all(shop["listing_type"] == "digital" for shop in payload["shops"]))
        self.assertFalse(payload["workers"]["image_api"]["enabled"])
        self.assertEqual("primary_ui", payload["workers"]["chatgpt"]["provider_role"])
        self.assertEqual("research", payload["workers"]["hermes"]["profile_aliases"]["reserach"])

    def test_create_is_idempotent_and_rejects_physical_listing(self) -> None:
        first = self._create()
        self.assertEqual(201, first.status_code, first.text)
        first_payload = first.json()
        self.assertTrue(first_payload["created"])
        job_id = first_payload["job"]["job_id"]
        self.assertEqual("digital", first_payload["job"]["payload"]["listing_type"])

        replay = self._create(payload={"brief": "changed replay must not replace original"})
        self.assertEqual(200, replay.status_code, replay.text)
        replay_payload = replay.json()
        self.assertFalse(replay_payload["created"])
        self.assertEqual(job_id, replay_payload["job"]["job_id"])
        self.assertEqual("weekly printable budget planner", replay_payload["job"]["payload"]["brief"])

        physical = self._create(
            idempotency_key="pipeline-idem-physical",
            listing_type="physical",
        )
        self.assertEqual(400, physical.status_code, physical.text)
        self.assertIn("listing_type=digital", physical.text)

    def test_ui_style_research_without_ids_gets_a_stable_candidate(self) -> None:
        first = self.client.post(
            "/api/pipeline/jobs",
            headers=self._headers(),
            json={
                "campaign_id": "camp-20260912",
                "shop_id": "daisyflowdigital",
                "stage": "research",
                "listing_type": "digital",
                "payload": {"brief": "  Printable   wedding budget planner  "},
            },
        )
        self.assertEqual(201, first.status_code, first.text)
        first_job = first.json()["job"]

        replay = self.client.post(
            "/api/pipeline/jobs",
            headers=self._headers(),
            json={
                "campaign_id": "camp-20260912",
                "shop_id": "daisyflowdigital",
                "stage": "research",
                "listing_type": "digital",
                "payload": {"brief": "Printable wedding budget planner"},
            },
        )
        self.assertEqual(200, replay.status_code, replay.text)
        replay_job = replay.json()["job"]
        self.assertFalse(replay.json()["created"])
        self.assertEqual(first_job["job_id"], replay_job["job_id"])
        self.assertEqual(first_job["candidate_id"], replay_job["candidate_id"])

    def test_worker_manifest_endpoint_binds_final_files_to_claimed_job(self) -> None:
        base_patch = patch.object(dashboard_app, "BASE_DIR", self._root)
        base_patch.start()
        self.addCleanup(base_patch.stop)
        product_root = self._root / "shops" / "templystudios" / "product-001"
        product_root.mkdir(parents=True)
        (product_root / "final.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
            '<rect x="1" y="1" width="98" height="98" /></svg>',
            encoding="utf-8",
        )
        build_vector_delivery_zip(
            product_root / "buyer.zip",
            ["final.svg"],
            product_id="product-001",
            campaign_id="camp-20260912",
            product_root=product_root.resolve(),
        )
        source_root = product_root / "source_assets"
        source_root.mkdir()
        (source_root / "reference.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><circle cx="10" cy="10" r="5" /></svg>',
            encoding="utf-8",
        )
        created = self._create(
            idempotency_key="pipeline-idem-manifest",
            stage="create-product",
            product_id="product-001",
        )
        self.assertEqual(201, created.status_code, created.text)
        job_id = created.json()["job"]["job_id"]
        claim = self.client.post(
            f"/api/pipeline/jobs/{job_id}/claim",
            headers=self._headers(),
            json={"worker_id": "hermes-create"},
        )
        self.assertEqual(200, claim.status_code, claim.text)
        fence = claim.json()["fence"]

        response = self.client.post(
            f"/api/pipeline/jobs/{job_id}/manifest",
            headers=self._headers(),
            json={
                "worker_id": "hermes-create",
                "fence": fence,
                "product_root": str(product_root.resolve()),
                "customer_files": ["buyer.zip"],
                "source_assets": ["source_assets/reference.svg"],
                "source_provenance": [
                    {"url": "https://www.creativefabrica.com/example", "license": "commercial-use"}
                ],
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertEqual(job_id, payload["manifest"]["pipeline_job_id"])
        self.assertEqual("templystudios", payload["manifest"]["shop_id"])
        self.assertEqual(["buyer.zip"], payload["manifest"]["customer_files"])
        self.assertEqual("digital", payload["manifest"]["listing_type"])
        self.assertTrue((product_root / ".pipeline-product.json").is_file())

        draft = self._create(
            idempotency_key="pipeline-idem-manifest-draft",
            stage="create-verified-draft",
            product_id="product-001",
            side_effect_boundary="etsy_write",
        )
        self.assertEqual(201, draft.status_code, draft.text)
        draft_job_id = draft.json()["job"]["job_id"]
        draft_claim = self.client.post(
            f"/api/pipeline/jobs/{draft_job_id}/claim",
            headers=self._headers(),
            json={"worker_id": "poster-worker"},
        )
        self.assertEqual(200, draft_claim.status_code, draft_claim.text)
        draft_fence = draft_claim.json()["fence"]
        image_dir = product_root / "images"
        image_dir.mkdir()
        image_paths = []
        for index in range(10):
            image_path = image_dir / f"image-{index}.png"
            Image.new("RGB", (1, 1), (index * 17 % 255, 64, 128)).save(image_path, format="PNG")
            image_paths.append(str(image_path))
        adopted = self.client.post(
            f"/api/pipeline/jobs/{draft_job_id}/manifest",
            headers=self._headers(),
            json={
                "worker_id": "poster-worker",
                "fence": draft_fence,
                "source_job_id": job_id,
                "product_root": str(product_root.resolve()),
                "image_files": [f"images/image-{index}.png" for index in range(10)],
            },
        )
        self.assertEqual(200, adopted.status_code, adopted.text)
        self.assertTrue(adopted.json()["adopted"])
        self.assertEqual(draft_job_id, adopted.json()["manifest"]["pipeline_job_id"])
        self.assertEqual(draft_job_id, adopted.json()["job"]["job_id"])
        adopted_replay = self.client.post(
            f"/api/pipeline/jobs/{draft_job_id}/manifest",
            headers=self._headers(),
            json={
                "worker_id": "poster-worker",
                "fence": draft_fence,
                "source_job_id": job_id,
                "product_root": str(product_root.resolve()),
                "image_files": [f"images/image-{index}.png" for index in range(10)],
            },
        )
        self.assertEqual(200, adopted_replay.status_code, adopted_replay.text)
        self.assertTrue(adopted_replay.json()["adopted"])

        # Read the adopted product through the same catalog loader as the
        # browser poster, then run its local pre-save/quota gate. No browser
        # or Etsy side effect is performed by this acceptance test.
        workbook_path = self._root / "shops" / "templystudios" / "Etsy_SEO_Generator.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Listings"
        for _ in range(3):
            sheet.append([])
        sheet.append([
            "", "product-001", "vector design", "", 4.99,
            "Cutting Machine Files", "", "Vector Product",
            "A buyer-facing vector product with https://www.creativefabrica.com/example evidence.",
            ", ".join(f"tag-{index}" for index in range(13)), 999, "", "2020_2026", "", "Digital", "", "", "SKU-001", "",
        ])
        workbook_path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(workbook_path)
        workbook.close()

        def fake_resolve(product, shop_id, store=None):
            assert shop_id == "templystudios"
            product["image_paths"] = list(image_paths)
            product["pdf_paths"] = [str(product_root / "buyer.zip")]
            product["asset_root"] = str(product_root)
            return {"source": "local", "asset_root": str(product_root), "image_paths": [], "file_paths": []}

        with patch.object(etsy_auto_post, "SHOP_DIR", self._root / "shops" / "templystudios"), patch.object(
            etsy_auto_post, "EXCEL_FILE", workbook_path
        ), patch.object(etsy_auto_post, "resolve_product_asset_paths", fake_resolve):
            products, _workbook, _sheet, total = etsy_auto_post.read_products(
                product_folder="product-001",
                shop_id="templystudios",
                store=object(),
            )
        self.assertEqual(1, total)
        poster_product = products[0]
        self.assertEqual(draft_job_id, poster_product["pipeline_job_id"])
        worker_id, poster_fence = etsy_auto_post._prepare_pipeline_post(
            poster_product, "templystudios", dashboard_app._ETSY_JOB_STORE
        )
        self.assertEqual("poster-worker", worker_id)
        self.assertGreater(poster_fence, draft_fence)
        self.assertEqual("held", dashboard_app._ETSY_JOB_STORE.get_pipeline_quota_reservation(draft_job_id)["status"])
        ready = dashboard_app._ETSY_JOB_STORE.prepare_pipeline_write(
            draft_job_id,
            worker_id=worker_id,
            fence=poster_fence,
            policy=dashboard_app.PIPELINE_POLICY,
        )
        self.assertEqual("write_ready", ready["reason"])
        receipt = etsy_auto_post._pipeline_verified_receipt(
            poster_product,
            "https://www.etsy.com/listing/123456789",
            worker_id=worker_id,
            fence=poster_fence,
        )
        completed = dashboard_app._ETSY_JOB_STORE.complete_verified(
            draft_job_id,
            worker_id=worker_id,
            fence=poster_fence,
            receipt=receipt,
            policy=dashboard_app.PIPELINE_POLICY,
        )
        self.assertEqual("verified", completed["job"]["status"])

        rejected = self.client.post(
            f"/api/pipeline/jobs/{job_id}/manifest",
            headers=self._headers(),
            json={
                "worker_id": "stale-worker",
                "fence": fence,
                "product_root": str(product_root.resolve()),
                "customer_files": ["source_assets/reference.svg"],
            },
        )
        self.assertEqual(409, rejected.status_code, rejected.text)

    def test_draft_manifest_adoption_recovers_after_bind_crash(self) -> None:
        """A crash after the atomic file replace must be retryable."""

        base_patch = patch.object(dashboard_app, "BASE_DIR", self._root)
        base_patch.start()
        self.addCleanup(base_patch.stop)
        product_root = self._root / "shops" / "templystudios" / "product-002"
        product_root.mkdir(parents=True)
        (product_root / "final.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
            '<rect x="1" y="1" width="98" height="98" /></svg>',
            encoding="utf-8",
        )
        build_vector_delivery_zip(
            product_root / "buyer.zip",
            ["final.svg"],
            product_id="product-002",
            campaign_id="camp-crash-recovery",
            product_root=product_root.resolve(),
        )
        source_root = product_root / "source_assets"
        source_root.mkdir()
        (source_root / "reference.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><circle cx="10" cy="10" r="5" /></svg>',
            encoding="utf-8",
        )
        image_dir = product_root / "images"
        image_dir.mkdir()
        for index in range(10):
            Image.new("RGB", (1, 1), (index * 19 % 255, 96, 160)).save(
                image_dir / f"image-{index}.png", format="PNG"
            )

        source = self._create(
            idempotency_key="pipeline-idem-crash-source",
            campaign_id="camp-crash-recovery",
            stage="create-product",
            product_id="product-002",
        )
        self.assertEqual(201, source.status_code, source.text)
        source_job_id = source.json()["job"]["job_id"]
        source_claim = self.client.post(
            f"/api/pipeline/jobs/{source_job_id}/claim",
            headers=self._headers(),
            json={"worker_id": "hermes-create"},
        )
        self.assertEqual(200, source_claim.status_code, source_claim.text)
        source_manifest = self.client.post(
            f"/api/pipeline/jobs/{source_job_id}/manifest",
            headers=self._headers(),
            json={
                "worker_id": "hermes-create",
                "fence": source_claim.json()["fence"],
                "product_root": str(product_root.resolve()),
                "customer_files": ["buyer.zip"],
                "source_assets": ["source_assets/reference.svg"],
                "source_provenance": [
                    {"url": "https://www.creativefabrica.com/crash-example", "license": "commercial-use"}
                ],
            },
        )
        self.assertEqual(200, source_manifest.status_code, source_manifest.text)

        draft = self._create(
            idempotency_key="pipeline-idem-crash-draft",
            campaign_id="camp-crash-recovery",
            stage="create-verified-draft",
            product_id="product-002",
            side_effect_boundary="etsy_write",
        )
        self.assertEqual(201, draft.status_code, draft.text)
        draft_job_id = draft.json()["job"]["job_id"]
        draft_claim = self.client.post(
            f"/api/pipeline/jobs/{draft_job_id}/claim",
            headers=self._headers(),
            json={"worker_id": "poster-worker"},
        )
        self.assertEqual(200, draft_claim.status_code, draft_claim.text)
        adoption_payload = {
            "worker_id": "poster-worker",
            "fence": draft_claim.json()["fence"],
            "source_job_id": source_job_id,
            "product_root": str(product_root.resolve()),
            "image_files": [f"images/image-{index}.png" for index in range(10)],
        }
        store = dashboard_app._ETSY_JOB_STORE
        self.assertIsNotNone(store)
        assert store is not None
        with patch.object(
            store,
            "record_pipeline_manifest",
            side_effect=dashboard_app.JobStoreError("simulated bind crash"),
        ):
            failed = self.client.post(
                f"/api/pipeline/jobs/{draft_job_id}/manifest",
                headers=self._headers(),
                json=adoption_payload,
            )
        self.assertEqual(409, failed.status_code, failed.text)
        self.assertEqual(
            draft_job_id,
            dashboard_app.read_pipeline_product_manifest(product_root)["pipeline_job_id"],
        )
        self.assertFalse(store.get_pipeline_job(draft_job_id).get("manifest_sha256"))

        retried = self.client.post(
            f"/api/pipeline/jobs/{draft_job_id}/manifest",
            headers=self._headers(),
            json=adoption_payload,
        )
        self.assertEqual(200, retried.status_code, retried.text)
        self.assertTrue(retried.json()["adopted"])
        self.assertEqual(draft_job_id, retried.json()["manifest"]["pipeline_job_id"])
        self.assertEqual(
            retried.json()["manifest_sha256"],
            store.get_pipeline_job(draft_job_id)["manifest_sha256"],
        )

    def test_fenced_worker_can_reserve_quota_and_only_complete_with_verified_receipt(self) -> None:
        created = self._create(stage="create-verified-draft", side_effect_boundary="etsy_write")
        self.assertEqual(201, created.status_code, created.text)
        job_id = created.json()["job"]["job_id"]

        claim = self.client.post(
            f"/api/pipeline/jobs/{job_id}/claim",
            headers=self._headers(),
            json={"worker_id": "mac-worker-01", "lease_seconds": 60},
        )
        self.assertEqual(200, claim.status_code, claim.text)
        fence = claim.json()["fence"]
        self.assertGreaterEqual(fence, 1)

        heartbeat = self.client.post(
            f"/api/pipeline/jobs/{job_id}/heartbeat",
            headers=self._headers(),
            json={"worker_id": "mac-worker-01", "fence": fence, "lease_seconds": 60},
        )
        self.assertEqual(200, heartbeat.status_code, heartbeat.text)

        quota_day = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()
        reservation = self.client.post(
            f"/api/pipeline/jobs/{job_id}/reserve-quota",
            headers=self._headers(),
            json={"quota_date": quota_day, "worker_id": "mac-worker-01", "fence": fence},
        )
        self.assertEqual(200, reservation.status_code, reservation.text)
        self.assertEqual("held", reservation.json()["reservation"]["status"])

        incomplete = self.client.post(
            f"/api/pipeline/jobs/{job_id}/transition",
            headers=self._headers(),
            json={"status": "verified", "worker_id": "mac-worker-01", "fence": fence, "receipt": {"listing_id": "123"}},
        )
        self.assertEqual(409, incomplete.status_code, incomplete.text)

        job = self.client.get(f"/api/pipeline/jobs/{job_id}").json()["job"]
        image_hashes = [hashlib.sha256(f"image-{i}".encode()).hexdigest() for i in range(10)]
        receipt = {
            "job_id": job_id,
            "campaign_id": job["campaign_id"],
            "candidate_id": job["candidate_id"],
            "shop_id": job["shop_id"],
            "product_id": job["product_id"],
            "worker_id": "mac-worker-01",
            "fence": fence,
            "listing_id": "1234567890",
            "listing_status": "draft",
            "readback_url": "https://www.etsy.com/listing/1234567890",
            "readback_ok": True,
            "image_hashes": image_hashes,
            "customer_file_hashes": [hashlib.sha256(b"buyer").hexdigest()],
            "manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
            "asset_qa_ok": True,
            "seo_validated": True,
            "customer_files_validated": True,
        }
        self._bind_synthetic_manifest(
            job_id,
            "mac-worker-01",
            fence,
            receipt["manifest_sha256"],
            receipt["customer_file_hashes"][0],
            receipt["image_hashes"],
        )
        verified = self.client.post(
            f"/api/pipeline/jobs/{job_id}/transition",
            headers=self._headers(),
            json={
                "status": "verified",
                "worker_id": "mac-worker-01",
                "fence": fence,
                "receipt": receipt,
                "side_effect_boundary": "etsy_write",
            },
        )
        self.assertEqual(200, verified.status_code, verified.text)
        self.assertEqual("verified", verified.json()["job"]["status"])
        self.assertEqual(10, len(verified.json()["job"]["receipt"]["image_hashes"]))
        quota = self.client.get("/api/pipeline/quota")
        self.assertEqual(200, quota.status_code, quota.text)
        self.assertEqual(1, quota.json()["used"])
        self.assertEqual(4, quota.json()["remaining"])

    def test_external_write_restart_enters_unknown_and_reconciliation_is_explicit(self) -> None:
        created = self._create(
            idempotency_key="pipeline-idem-unknown",
            stage="create-verified-draft",
            product_id="product-unknown",
            side_effect_boundary="etsy_write",
        )
        self.assertEqual(201, created.status_code, created.text)
        job_id = created.json()["job"]["job_id"]
        claim = self.client.post(
            f"/api/pipeline/jobs/{job_id}/claim",
            headers=self._headers(),
            json={"worker_id": "mac-worker-01"},
        )
        self.assertEqual(200, claim.status_code, claim.text)

        store = dashboard_app._ETSY_JOB_STORE
        self.assertIsNotNone(store)
        assert store is not None
        store._conn.execute("UPDATE pipeline_jobs SET lease_expires_at = 0 WHERE id = ?", (job_id,))
        store._conn.commit()
        recovery = store.recover_pipeline_jobs(reason="test_restart")
        self.assertEqual({"queued": 0, "unknown": 1}, recovery)
        current = self.client.get(f"/api/pipeline/jobs/{job_id}")
        self.assertEqual("unknown", current.json()["job"]["status"])

        # A receipt cannot be smuggled into the begin call.  The failed
        # request must leave the job UNKNOWN so an operator can retry the
        # handshake without manually repairing state.
        invalid_one_call = self.client.post(
            f"/api/pipeline/jobs/{job_id}/reconcile",
            headers=self._headers(),
            json={"worker_id": "mac-worker-01", "receipt": {}},
        )
        self.assertEqual(400, invalid_one_call.status_code, invalid_one_call.text)
        self.assertEqual(
            "unknown",
            self.client.get(f"/api/pipeline/jobs/{job_id}").json()["job"]["status"],
        )

        reconcile = self.client.post(
            f"/api/pipeline/jobs/{job_id}/reconcile",
            headers=self._headers(),
            json={"reason": "read Etsy draft before retry"},
        )
        self.assertEqual(200, reconcile.status_code, reconcile.text)
        self.assertEqual("reconciling", reconcile.json()["job"]["status"])

        claimed = self.client.post(
            f"/api/pipeline/jobs/{job_id}/claim",
            headers=self._headers(),
            json={"worker_id": "reconcile-worker"},
        )
        self.assertEqual(200, claimed.status_code, claimed.text)
        fence = claimed.json()["fence"]
        reservation = self.client.post(
            f"/api/pipeline/jobs/{job_id}/reserve-quota",
            headers=self._headers(),
            json={"worker_id": "reconcile-worker", "fence": fence},
        )
        self.assertEqual(200, reservation.status_code, reservation.text)
        receipt = {
            "job_id": job_id,
            "campaign_id": "camp-20260912",
            "candidate_id": "candidate-001",
            "shop_id": "templystudios",
            "product_id": "product-unknown",
            "worker_id": "reconcile-worker",
            "fence": fence,
            "listing_id": "1234567890",
            "listing_status": "draft",
            "readback_url": "https://www.etsy.com/listing/1234567890",
            "readback_ok": True,
            "image_hashes": [hashlib.sha256(f"image-{i}".encode()).hexdigest() for i in range(10)],
            "customer_file_hashes": [hashlib.sha256(b"buyer").hexdigest()],
            "manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
            "asset_qa_ok": True,
            "seo_validated": True,
            "customer_files_validated": True,
        }
        self._bind_synthetic_manifest(
            job_id,
            "reconcile-worker",
            fence,
            receipt["manifest_sha256"],
            receipt["customer_file_hashes"][0],
            receipt["image_hashes"],
        )
        completed = self.client.post(
            f"/api/pipeline/jobs/{job_id}/reconcile/complete",
            headers=self._headers(),
            json={"worker_id": "reconcile-worker", "fence": fence, "receipt": receipt},
        )
        self.assertEqual(200, completed.status_code, completed.text)
        self.assertEqual("verified", completed.json()["job"]["status"])

    def test_publish_stage_is_not_admitted_by_the_approval_gated_v1(self) -> None:
        response = self._create(stage="publish", idempotency_key="pipeline-idem-publish")

        self.assertEqual(400, response.status_code, response.text)
        self.assertIn("stage", response.text)

    def test_anna_outbox_requires_bridge_token_and_ack_is_idempotent(self) -> None:
        denied = self.client.get("/api/pipeline/outbox", headers={"Host": "127.0.0.1:8090"})
        self.assertEqual(401, denied.status_code, denied.text)
        created = self._create(idempotency_key="pipeline-idem-outbox")
        self.assertEqual(201, created.status_code, created.text)
        event_id = created.json()["event"]["event_id"]
        headers = {**self._headers(), "x-anna-bridge-token": "anna-bridge-test-token"}
        pending = self.client.get("/api/pipeline/outbox", headers=headers)
        self.assertEqual(200, pending.status_code, pending.text)
        self.assertTrue(any(item["event_id"] == event_id for item in pending.json()["events"]))
        ack = self.client.post(f"/api/pipeline/outbox/{event_id}/ack", headers=headers, json={})
        self.assertEqual(200, ack.status_code, ack.text)
        self.assertEqual([], [item for item in self.client.get("/api/pipeline/outbox", headers=headers).json()["events"] if item["event_id"] == event_id])


if __name__ == "__main__":
    unittest.main()
