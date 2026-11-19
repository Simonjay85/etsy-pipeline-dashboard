from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest
from PIL import Image

import etsy_auto_post
from pipeline_artifacts import build_vector_delivery_zip, write_pipeline_product_manifest
from job_store import JobStore, JobStoreError


def _valid_svg(path: Path) -> Path:
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        '<path d="M0 0h10v10H0z"/></svg>',
        encoding="utf-8",
    )
    return path


def test_strict_manifest_paths_are_loaded_exactly_for_the_poster(tmp_path):
    shop_root = tmp_path / "shops" / "templystudios"
    product_root = shop_root / "product-1"
    product_root.mkdir(parents=True)
    svg = _valid_svg(product_root / "design.svg")
    buyer = build_vector_delivery_zip(
        product_root / "buyer.zip",
        [svg],
        product_id="product-1",
        campaign_id="campaign-1",
        product_root=product_root,
    )
    write_pipeline_product_manifest(
        product_root,
        pipeline_job_id="pipeline-job-1",
        campaign_id="campaign-1",
        candidate_id="candidate-1",
        product_id="product-1",
        shop_id="templystudios",
        customer_files=[buyer["archive"]["path"]],
    )

    workbook_path = shop_root / "Etsy_SEO_Generator.xlsx"
    workbook_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Listings"
    sheet.append([])
    sheet.append([])
    sheet.append([])
    sheet.append([
        "",
        "product-1",
        "vector design",
        "",
        4.99,
        "Cutting Machine Files",
        "",
        "Original Vector Design",
        "A buyer-facing product description",
        "tag1, tag2",
        999,
        "",
        "2020_2026",
        "",
        "Digital",
        "",
        "",
        "SKU-1",
    ])
    workbook.save(workbook_path)

    def fake_resolve(product, shop_id, store=None):
        assert shop_id == "templystudios"
        assert product["customer_files"] == [str(product_root / "buyer.zip")]
        product["image_paths"] = [str(product_root / f"image-{index}.png") for index in range(10)]
        product["pdf_paths"] = [str(product_root / "buyer.zip")]
        product["asset_root"] = str(product_root)
        return {"source": "local", "asset_root": str(product_root), "image_paths": [], "file_paths": []}

    with patch.object(etsy_auto_post, "SHOP_DIR", shop_root), patch.object(
        etsy_auto_post, "EXCEL_FILE", workbook_path
    ), patch.object(etsy_auto_post, "resolve_product_asset_paths", fake_resolve):
        products, _workbook, _sheet, total = etsy_auto_post.read_products(
            product_folder="product-1",
            shop_id="templystudios",
            store=object(),
        )

    assert total == 1
    assert products[0]["pipeline_job_id"] == "pipeline-job-1"
    assert products[0]["pipeline_campaign_id"] == "campaign-1"
    assert products[0]["pipeline_candidate_id"] == "candidate-1"
    assert products[0]["pipeline_product_id"] == "product-1"
    assert products[0]["pipeline_shop_id"] == "templystudios"
    assert products[0]["customer_files"] == [str(product_root / "buyer.zip")]


def test_prepare_pipeline_post_rejects_manifest_identity_mismatch(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite")
    try:
        job, _ = store.create_pipeline_job(
            campaign_id="campaign-1",
            shop_id="templystudios",
            stage="create-verified-draft",
            product_id="product-1",
            candidate_id="candidate-1",
            input_version="1",
            source_fingerprint="source-1",
            payload={"listing_type": "digital"},
            idempotency_key="prepare-mismatch",
            contract_version="1.0",
            side_effect_boundary="etsy_write",
        )
        product = {
            "pipeline_job_id": job["job_id"],
            "pipeline_campaign_id": "campaign-other",
            "pipeline_candidate_id": "candidate-1",
            "pipeline_shop_id": "templystudios",
            "pipeline_product_id": "product-1",
            "pipeline_contract_version": "1.0",
        }
        try:
            etsy_auto_post._prepare_pipeline_post(product, "templystudios", store)
        except JobStoreError as exc:
            assert "không khớp" in str(exc)
        else:  # pragma: no cover - the guard is the behavior under test
            raise AssertionError("mismatched manifest identity was accepted")
        assert store.get_pipeline_job(job["job_id"])["status"] == "queued"
    finally:
        store.close()


def test_prepare_pipeline_post_requires_dashboard_accepted_manifest(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite")
    try:
        job, _ = store.create_pipeline_job(
            campaign_id="campaign-1",
            shop_id="templystudios",
            stage="create-verified-draft",
            product_id="product-1",
            candidate_id="candidate-1",
            input_version="1",
            source_fingerprint="source-1",
            payload={"listing_type": "digital"},
            idempotency_key="prepare-no-manifest",
            contract_version="1.0",
            side_effect_boundary="etsy_write",
        )
        product = {
            "pipeline_job_id": job["job_id"],
            "pipeline_campaign_id": "campaign-1",
            "pipeline_candidate_id": "candidate-1",
            "pipeline_shop_id": "templystudios",
            "pipeline_product_id": "product-1",
            "pipeline_contract_version": "1.0",
        }
        try:
            etsy_auto_post._prepare_pipeline_post(product, "templystudios", store)
        except JobStoreError as exc:
            assert "manifest evidence" in str(exc)
        else:  # pragma: no cover - the guard is the behavior under test
            raise AssertionError("draft without accepted manifest was admitted")
        assert store.get_pipeline_job(job["job_id"])["status"] == "queued"
    finally:
        store.close()


def test_prepare_pipeline_post_rejects_resolver_image_set_before_claim(tmp_path):
    """A resolver/cache substitution must stop before quota or Etsy Save."""

    image_root = tmp_path / "images"
    image_root.mkdir()
    expected_paths = []
    wrong_paths = []
    expected_hashes = []
    for index in range(10):
        expected = image_root / f"image-{index}.png"
        wrong = image_root / f"wrong-{index}.png"
        Image.new("RGB", (2, 2), (index * 13 % 255, 40, 90)).save(expected, format="PNG")
        Image.new("RGB", (2, 2), (200, index * 17 % 255, 20)).save(wrong, format="PNG")
        expected_paths.append(str(expected))
        wrong_paths.append(str(wrong))
        expected_hashes.append(hashlib.sha256(expected.read_bytes()).hexdigest())

    job = {
        "job_id": "draft-image-selection",
        "campaign_id": "campaign-1",
        "candidate_id": "candidate-1",
        "shop_id": "templystudios",
        "product_id": "product-1",
        "contract_version": "1.0",
        "stage": "create-verified-draft",
        "manifest_sha256": "a" * 64,
        "manifest_evidence": {
            "image_files": [f"images/image-{index}.png" for index in range(10)],
            "image_file_evidence": [
                {
                    "path": f"images/image-{index}.png",
                    "sha256": expected_hashes[index],
                    "size": 1,
                }
                for index in range(10)
            ],
            "customer_file_evidence": [{"path": "buyer.zip", "sha256": "b" * 64, "size": 1}],
        },
    }

    class StubStore:
        def __init__(self):
            self.claim_calls = 0

        def get_pipeline_job(self, job_id):
            assert job_id == job["job_id"]
            return job

        def claim_pipeline_job(self, *args, **kwargs):  # pragma: no cover - must never be reached
            self.claim_calls += 1
            raise AssertionError("image selection must fail before claim")

    store = StubStore()
    product = {
        "pipeline_job_id": job["job_id"],
        "pipeline_campaign_id": job["campaign_id"],
        "pipeline_candidate_id": job["candidate_id"],
        "pipeline_shop_id": job["shop_id"],
        "pipeline_product_id": job["product_id"],
        "pipeline_contract_version": job["contract_version"],
        "pipeline_image_files": [f"images/image-{index}.png" for index in range(10)],
        # Simulate the asset resolver returning a different valid ten-image set.
        "image_paths": wrong_paths,
        "asset_root": str(tmp_path),
    }
    with pytest.raises(JobStoreError, match="image"):
        etsy_auto_post._prepare_pipeline_post(product, "templystudios", store)
    assert store.claim_calls == 0
