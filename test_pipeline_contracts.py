#!/usr/bin/env python3
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from pipeline_contracts import (
    PipelineContractError,
    PipelineStage,
    build_idempotency_key,
    source_fingerprint,
    validate_customer_files,
    validate_final_artifact,
    validate_seo_payload,
)


def _write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _write_text(path: Path, data: str) -> None:
    path.write_text(data, encoding="utf-8")


def test_validate_customer_files_accepts_pdf_and_zip(tmp_path: Path) -> None:
    pdf_path = tmp_path / "artifact" / "listing.pdf"
    zip_path = tmp_path / "artifact" / "bundle.zip"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes(pdf_path, b"%PDF-1.7\n1 0 obj\n>>\nendobj\n%%EOF")
    _write_bytes(zip_path, b"PK\x03\x04")

    result = validate_customer_files([pdf_path, zip_path], max_file_size_bytes=20 * 1024 * 1024)
    assert len(result) == 2
    assert set(path.suffix.lower() for path in result) == {".pdf", ".zip"}


def test_validate_customer_files_rejects_more_than_five(tmp_path: Path) -> None:
    paths = []
    for idx in range(6):
        path = tmp_path / f"f{idx}.pdf"
        _write_bytes(path, b"%PDF-1.7\n%%EOF")
        paths.append(path)
    with pytest.raises(PipelineContractError):
        validate_customer_files(paths)


def test_validate_customer_files_rejects_source_asset(tmp_path: Path) -> None:
    source_dir = tmp_path / "some" / "source_assets"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_pdf = source_dir / "source.pdf"
    _write_bytes(source_pdf, b"%PDF-1.7\n%%EOF")
    with pytest.raises(PipelineContractError, match="Source asset path"):
        validate_customer_files([source_pdf])


def test_validate_customer_files_rejects_too_large_file(tmp_path: Path) -> None:
    path = tmp_path / "huge.pdf"
    _write_bytes(path, b"%PDF-1.7\n" + b"a" * (20 * 1024 * 1024))
    with pytest.raises(PipelineContractError, match="exceeds max size"):
        validate_customer_files([path], max_file_size_bytes=10 * 1024 * 1024)


def test_validate_customer_files_rejects_invalid_extension(tmp_path: Path) -> None:
    path = tmp_path / "bad.txt"
    _write_text(path, "not allowed")
    with pytest.raises(PipelineContractError, match="Unsupported file extension"):
        validate_customer_files([path])


def test_validate_seo_payload_requires_exactly_thirteen_tags_and_evidence() -> None:
    payload = validate_seo_payload(
        title="Printable minimalist wedding invitation card design",
        description="Based on competitor scan: https://competitor.example/listing-1 and https://competitor.example/listing-2",
        tags=[
            "minimalist",
            "digital",
            "invitation",
            "wedding",
            "template",
            "editable",
            "printable",
            "vector",
            "poster",
            "instant download",
            "premium",
            "customizable",
            "commercial use",
        ],
        evidence_urls=[
            "https://competitor.example/listing-1",
            "https://competitor.example/listing-2",
        ],
    )
    assert payload["tag_count"] == 13
    assert payload["title"] == "Printable minimalist wedding invitation card design"


def test_validate_seo_payload_rejects_non_evidence_description() -> None:
    with pytest.raises(PipelineContractError, match="must reference at least one evidence"):
        validate_seo_payload(
            title="Title",
            description="Draft prepared from internal reasoning only, with enough detail but no external evidence references.",
            tags=[f"tag{idx}" for idx in range(13)],
            evidence_urls=["https://example.com/ref"],
        )


def test_source_fingerprint_and_idempotency() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as workspace:
        base = Path(workspace)
        first = base / "first.txt"
        second = base / "second.txt"
        _write_bytes(first, b"same")
        _write_bytes(second, b"same")
        fp1 = source_fingerprint(first)
        fp2 = source_fingerprint(second)
        assert fp1 == fp2

        fp1b = source_fingerprint(base)
        fp2b = source_fingerprint(base)
        assert fp1b == fp2b

        key_a = build_idempotency_key(
            campaign_id="camp-1",
            shop_id="templystudios",
            product_id="product-1",
            stage=PipelineStage.RESEARCH,
            input_version="v1",
            source_fingerprint_value=fp1,
        )
        key_b = build_idempotency_key(
            campaign_id="camp-1",
            shop_id="templystudios",
            product_id="product-1",
            stage=PipelineStage.RESEARCH,
            input_version="v1",
            source_fingerprint_value=fp1,
        )
        assert key_a == key_b


def test_validate_final_artifact_rejects_3d_dxf(tmp_path: Path) -> None:
    dxf = tmp_path / "bad.dxf"
    _write_text(
        dxf,
        "  0\nSECTION\n  2\nENTITIES\n  0\n3DFACE\n  8\nLayer\n  0\nEOF",
    )
    with pytest.raises(PipelineContractError, match="3D geometry"):
        validate_final_artifact(dxf)


def test_validate_final_artifact_zip_traversal_rejected(tmp_path: Path) -> None:
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../outside.txt", "bad")
        zf.writestr("product.pdf", "%PDF-1.7\n%%EOF")
    with pytest.raises(PipelineContractError, match="traversal"):
        validate_final_artifact(zip_path)


def test_validate_final_artifact_zip_source_assets_rejected(tmp_path: Path) -> None:
    zip_path = tmp_path / "source-leak.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("source_assets/original.svg", "<svg />")
    with pytest.raises(PipelineContractError, match="source_assets"):
        validate_final_artifact(zip_path)


def test_validate_final_artifact_zip_rechecks_contained_vector(tmp_path: Path) -> None:
    zip_path = tmp_path / "bad-vector.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("design.svg", "<svg viewBox='0 0 100 100'><script>bad</script></svg>")
    with pytest.raises(PipelineContractError, match="script"):
        validate_final_artifact(zip_path)


def test_validate_final_artifact_svg_rejects_script(tmp_path: Path) -> None:
    svg = tmp_path / "bad.svg"
    _write_text(svg, "<svg viewBox='0 0 100 100'><script>alert('x')</script></svg>")
    with pytest.raises(PipelineContractError, match="Embedded script"):
        validate_final_artifact(svg)


def test_validate_final_artifact_svg_rejects_data_and_invalid_dimensions(tmp_path: Path) -> None:
    svg = tmp_path / "bad-href.svg"
    _write_text(svg, "<svg viewBox='0 0 0 100'><image href='data:image/png;base64,AA==' /></svg>")
    with pytest.raises(PipelineContractError):
        validate_final_artifact(svg)


def test_validate_final_artifact_dxf_rejects_nonzero_z_and_malformed_pairs(tmp_path: Path) -> None:
    z_dxf = tmp_path / "z.dxf"
    _write_text(z_dxf, "0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n0\n20\n0\n30\n1\n11\n1\n21\n1\n0\nENDSEC\n0\nEOF\n")
    with pytest.raises(PipelineContractError, match="Z|elevation"):
        validate_final_artifact(z_dxf)
    malformed = tmp_path / "malformed.dxf"
    _write_text(malformed, "0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n0\nEOF\n")
    with pytest.raises(PipelineContractError, match="group-code"):
        validate_final_artifact(malformed)


def test_validate_final_artifact_rejects_pdf_without_header(tmp_path: Path) -> None:
    pdf = tmp_path / "bad.pdf"
    _write_bytes(pdf, b"NOTPDF")
    with pytest.raises(PipelineContractError, match="PDF header missing"):
        validate_final_artifact(pdf)


def test_validate_final_artifact_accepts_valid_dxf(tmp_path: Path) -> None:
    dxf = tmp_path / "good.dxf"
    _write_text(
        dxf,
        "  0\nSECTION\n  2\nENTITIES\n  0\nLINE\n 10\n0.0\n 11\n1.0\n 20\n1.0\n  0\nENDSEC\n  0\nEOF",
    )
    evidence = validate_final_artifact(dxf)
    assert evidence.format_hint == "dxf"
    assert os.path.exists(evidence.file_path)
