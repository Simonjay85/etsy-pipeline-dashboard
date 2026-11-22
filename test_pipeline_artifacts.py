from __future__ import annotations

import json
import zipfile

import pytest

from pipeline_artifacts import (
    build_vector_delivery_zip,
    read_pipeline_product_manifest,
    write_pipeline_product_manifest,
)
from pipeline_contracts import PipelineContractError, validate_final_artifact


def _svg(path, name="design.svg"):
    target = path / name
    target.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M0 0h10v10H0z"/></svg>')
    return target


def _dxf(path):
    target = path / "design.dxf"
    target.write_text("0\nSECTION\n2\nENTITIES\n0\nLINE\n8\n0\n0\nENDSEC\n0\nEOF\n")
    return target


def test_vector_zip_contains_only_final_2d_files_and_manifest(tmp_path):
    svg = _svg(tmp_path)
    dxf = _dxf(tmp_path)
    manifest = build_vector_delivery_zip(tmp_path / "buyer.zip", [svg, dxf], product_id="p1", campaign_id="c1", product_root=tmp_path)
    assert manifest["source_assets_included"] is False
    validate_final_artifact(tmp_path / "buyer.zip")
    with zipfile.ZipFile(tmp_path / "buyer.zip") as archive:
        assert set(archive.namelist()) == {"design.svg", "design.dxf", "manifest.json"}
        manifest_text = archive.read("manifest.json").decode()
        assert "source_assets/" not in manifest_text


def test_product_manifest_separates_source_assets_and_allowlists_buyer_files(tmp_path):
    svg = _svg(tmp_path)
    buyer = build_vector_delivery_zip(tmp_path / "buyer.zip", [svg], product_id="p1", campaign_id="c1", product_root=tmp_path)
    source_dir = tmp_path / "source_assets"
    source_dir.mkdir()
    source = source_dir / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("reference.svg", svg.read_text())
    manifest = write_pipeline_product_manifest(
        tmp_path,
        campaign_id="c1",
        candidate_id="candidate-1",
        product_id="p1",
        pipeline_job_id="pipeline-job-1",
        shop_id="templystudios",
        customer_files=[buyer["archive"]["path"]],
        source_assets=[source],
        source_provenance=[{"url": "https://creativefabrica.com/item/1", "license": "subscription-license", "author": "Author", "observed_at": "2026-09-12T00:00:00Z"}],
    )
    assert manifest["customer_files"] == ["buyer.zip"]
    assert manifest["pipeline_job_id"] == "pipeline-job-1"
    assert manifest["shop_id"] == "templystudios"
    assert manifest["source_assets"][0]["path"] == "source_assets/source.zip"
    loaded = read_pipeline_product_manifest(tmp_path)
    assert loaded["source_policy"]["customer_files_only"] is True


def test_source_asset_cannot_be_customer_file(tmp_path):
    source_dir = tmp_path / "source_assets"
    source_dir.mkdir()
    source = _svg(source_dir)
    with pytest.raises(PipelineContractError):
        write_pipeline_product_manifest(
            tmp_path,
            campaign_id="c",
            candidate_id="x",
            product_id="p",
            pipeline_job_id="pipeline-job-1",
            shop_id="templystudios",
            customer_files=[source],
        )


def test_manifest_reader_rechecks_hash_and_artifact_after_write(tmp_path):
    svg = _svg(tmp_path)
    buyer = build_vector_delivery_zip(tmp_path / "buyer.zip", [svg], product_id="p1", campaign_id="c1", product_root=tmp_path)
    write_pipeline_product_manifest(
        tmp_path,
        campaign_id="c1",
        candidate_id="candidate-1",
        product_id="p1",
        pipeline_job_id="pipeline-job-1",
        shop_id="templystudios",
        customer_files=[buyer["archive"]["path"]],
    )
    (tmp_path / "buyer.zip").write_bytes(b"not a zip")
    with pytest.raises(PipelineContractError, match="hash/size|Invalid ZIP"):
        read_pipeline_product_manifest(tmp_path)


def test_product_manifest_requires_job_binding_and_registered_shop(tmp_path):
    svg = _svg(tmp_path)
    buyer = build_vector_delivery_zip(
        tmp_path / "buyer.zip",
        [svg],
        product_id="p1",
        campaign_id="c1",
        product_root=tmp_path,
    )
    write_pipeline_product_manifest(
        tmp_path,
        campaign_id="c1",
        candidate_id="candidate-1",
        product_id="p1",
        pipeline_job_id="pipeline-job-1",
        shop_id="templystudios",
        customer_files=[buyer["archive"]["path"]],
    )
    manifest_path = tmp_path / ".pipeline-product.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.pop("pipeline_job_id")
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PipelineContractError, match="pipeline_job_id"):
        read_pipeline_product_manifest(tmp_path)


def test_buyer_zip_rejects_unallowlisted_members_and_casefold_duplicates(tmp_path):
    svg = _svg(tmp_path)
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("design.svg", svg.read_text())
        archive.writestr("installer.exe", b"not a buyer deliverable")
    with pytest.raises(PipelineContractError, match="not an allowed buyer artifact"):
        validate_final_artifact(archive_path)

    duplicate_path = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate_path, "w") as archive:
        archive.writestr("design.svg", svg.read_text())
        archive.writestr("DESIGN.SVG", svg.read_text())
    with pytest.raises(PipelineContractError, match="duplicate member names"):
        validate_final_artifact(duplicate_path)


def test_vector_builder_records_flat_archive_member_and_preserves_destination_on_failure(tmp_path):
    nested = tmp_path / "vectors"
    nested.mkdir()
    svg = _svg(nested, "Design.SVG")
    manifest = build_vector_delivery_zip(
        tmp_path / "buyer.zip",
        [svg],
        product_id="p1",
        campaign_id="c1",
        product_root=tmp_path,
    )
    assert manifest["files"][0]["path"] == "Design.SVG"
    with zipfile.ZipFile(tmp_path / "buyer.zip") as archive:
        embedded = json.loads(archive.read("manifest.json"))
    assert embedded["files"][0]["path"] == "Design.SVG"

    destination = tmp_path / "existing.zip"
    destination.write_bytes(b"previous-valid-or-operator-owned-file")
    upper = _svg(tmp_path, "DESIGN.svg")
    with pytest.raises(PipelineContractError, match="trùng basename"):
        build_vector_delivery_zip(
            destination,
            [svg, upper],
            product_id="p1",
            campaign_id="c1",
            product_root=tmp_path,
        )
    assert destination.read_bytes() == b"previous-valid-or-operator-owned-file"
