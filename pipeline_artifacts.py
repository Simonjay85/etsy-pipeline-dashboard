"""Durable product manifests and safe vector delivery packaging."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from pipeline_contracts import PipelineContractError, validate_customer_files, validate_final_artifact

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # pragma: no cover - Pillow is part of the Etsy runtime
    Image = None
    UnidentifiedImageError = Exception


# Keep this allowlist local rather than importing JobStore.  ``job_store``
# imports the pipeline contracts, and making the artifact helper depend on the
# store would create a circular import for callers that only need validation.
_PIPELINE_ALLOWED_SHOPS = frozenset(
    {"templystudios", "daisyflowdigital", "shop3", "BubbleByJulie"}
)
_PIPELINE_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})


def _manifest_identity(value: Any, label: str, *, required: bool = True) -> str:
    """Normalize a manifest identity without accepting control characters."""

    normalized = str(value or "").strip()
    if required and not normalized:
        raise PipelineContractError(f"Pipeline manifest {label} is required")
    if len(normalized) > 256:
        raise PipelineContractError(f"Pipeline manifest {label} is too long")
    if any(ord(character) < 32 for character in normalized):
        raise PipelineContractError(f"Pipeline manifest {label} contains control characters")
    return normalized


def _confined_file(root: Path, raw: str | Path, *, source: str) -> tuple[Path, str]:
    root_input = root.expanduser()
    if root_input.is_symlink():
        raise PipelineContractError(f"Product root is a symlink: {root_input}")
    root = root_input.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    cursor = path
    while cursor != root and cursor != cursor.parent:
        if cursor.is_symlink():
            raise PipelineContractError(f"{source} path contains a symlink: {cursor}")
        cursor = cursor.parent
    if path.is_symlink():
        raise PipelineContractError(f"{source} file is a symlink: {path}")
    path = path.resolve()
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise PipelineContractError(f"{source} file is outside product root: {path}") from exc
    if not path.is_file():
        raise PipelineContractError(f"{source} file is missing or unsafe: {path}")
    if source == "customer" and any(part.casefold() == "source_assets" for part in Path(rel).parts):
        raise PipelineContractError(f"Source asset cannot be a customer file: {rel}")
    return path, rel


def _file_evidence(path: Path, rel: str) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _validate_listing_image(path: Path, rel: str) -> None:
    """Validate one final gallery image before it becomes immutable evidence."""

    if path.suffix.casefold() not in _PIPELINE_IMAGE_EXTENSIONS:
        raise PipelineContractError(f"Listing image format is not supported: {rel}")
    size = path.stat().st_size
    if size <= 0 or size > 20 * 1024 * 1024:
        raise PipelineContractError(f"Listing image size is invalid: {rel}")
    if Image is None:
        # The production requirements include Pillow. Keep a conservative
        # non-empty fallback for tools that import this module without it.
        return
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0:
                raise PipelineContractError(f"Listing image dimensions are invalid: {rel}")
            image.verify()
    except PipelineContractError:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise PipelineContractError(f"Listing image cannot be decoded: {rel}") from exc


def build_vector_delivery_zip(
    output_zip: str | Path,
    files: Iterable[str | Path],
    *,
    product_id: str,
    campaign_id: str,
    product_root: str | Path | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a buyer ZIP containing only final 2D SVG/DXF files and a manifest."""

    destination = Path(output_zip).expanduser().resolve()
    if destination.suffix.lower() != ".zip":
        raise PipelineContractError("Vector delivery archive must end in .zip")
    root_input = Path(product_root or destination.parent).expanduser()
    if root_input.is_symlink():
        raise PipelineContractError(f"Product root is a symlink: {root_input}")
    root = root_input.resolve()
    selected: list[tuple[Path, str]] = []
    seen: set[str] = set()
    archive_names: set[str] = set()
    for raw in files:
        path, rel = _confined_file(root, raw, source="customer")
        if path.suffix.lower() not in {".svg", ".dxf"}:
            raise PipelineContractError(f"Vector ZIP accepts only SVG/DXF final files: {rel}")
        if rel in seen:
            continue
        archive_name = Path(rel).name
        archive_key = archive_name.casefold()
        if archive_key in archive_names:
            raise PipelineContractError(
                f"Vector ZIP có hai file trùng basename, không thể đối soát: {archive_name}"
            )
        # Validate the source file before it is copied into the buyer archive.
        validate_final_artifact(path)
        seen.add(rel)
        archive_names.add(archive_key)
        selected.append((path, rel))
    if not selected:
        raise PipelineContractError("Vector delivery ZIP needs at least one SVG or DXF")

    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "etsy.pipeline-delivery-manifest.v1",
        "campaign_id": str(campaign_id),
        "product_id": str(product_id),
        "listing_type": "digital",
        "source_assets_included": False,
        # Evidence names the member that the buyer actually receives.  The
        # source path may be nested under the product root, but the archive is
        # intentionally flat; recording ``rel`` here would make the embedded
        # manifest disagree with the ZIP contents.
        "files": [_file_evidence(path, Path(rel).name) for path, rel in selected],
        "metadata": dict(metadata or {}),
    }
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".zip", dir=str(destination.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, rel in selected:
                archive.write(path, arcname=Path(rel).name)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
        # Validate the temporary archive before replacing any existing buyer
        # file.  A rejected duplicate/corrupt archive must never be left at
        # the destination path as if it were a usable deliverable.
        evidence = validate_final_artifact(temp)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    manifest["archive"] = {"path": str(destination), "sha256": evidence.file_sha256, "size": evidence.file_size_bytes}
    return manifest


def write_pipeline_product_manifest(
    product_root: str | Path,
    *,
    campaign_id: str,
    candidate_id: str,
    product_id: str,
    pipeline_job_id: str,
    shop_id: str,
    worker_id: str | None = None,
    customer_files: Iterable[str | Path],
    image_files: Iterable[str | Path] = (),
    source_assets: Iterable[str | Path] = (),
    source_provenance: Iterable[Mapping[str, Any]] = (),
    listing_type: str = "digital",
    contract_version: str = "1.0",
) -> dict[str, Any]:
    """Write the allowlist consumed by the poster and catalog importer.

    ``customer_files`` may contain only the buyer-facing PDF/ZIP outputs.  Raw
    Creative Fabrica sources are recorded separately under ``source_assets``
    and can never be selected for Etsy upload.
    """

    root_input = Path(product_root).expanduser()
    if root_input.is_symlink():
        raise PipelineContractError(f"Product root is a symlink: {root_input}")
    root = root_input.resolve()
    if not root.is_dir():
        raise PipelineContractError(f"Product root is missing or unsafe: {root}")
    if listing_type.strip().lower() != "digital":
        raise PipelineContractError("Pipeline products must use listing_type=digital")

    # A product manifest is executable input to the Etsy poster.  Bind it to
    # the durable JobStore identity before writing it so a helper-created
    # artifact cannot silently fall through the legacy posting path.  The
    # candidate/campaign/product values are also retained as an audit trail;
    # the JobStore verifies them again when the draft receipt is completed.
    manifest_job_id = _manifest_identity(pipeline_job_id, "pipeline_job_id")
    manifest_shop_id = _manifest_identity(shop_id, "shop_id")
    if manifest_shop_id not in _PIPELINE_ALLOWED_SHOPS:
        raise PipelineContractError(f"Unsupported pipeline shop_id: {manifest_shop_id}")
    manifest_campaign_id = _manifest_identity(campaign_id, "campaign_id")
    manifest_candidate_id = _manifest_identity(candidate_id, "candidate_id")
    manifest_product_id = _manifest_identity(product_id, "product_id")
    manifest_worker_id = _manifest_identity(worker_id, "worker_id", required=False)

    customer_paths: list[tuple[Path, str]] = []
    for raw in customer_files:
        customer_paths.append(_confined_file(root, raw, source="customer"))
    checked_customer = validate_customer_files([path for path, _ in customer_paths])
    # A manifest is the promotion gate for downstream image/SEO/posting
    # workers, so extension and size checks alone are insufficient.  Validate
    # every buyer-facing artifact before it becomes allowlisted.
    for path in checked_customer:
        validate_final_artifact(path)
    source_paths: list[tuple[Path, str]] = []
    for raw in source_assets:
        source_paths.append(_confined_file(root, raw, source="source"))
    customer_names = {rel for _, rel in customer_paths}
    if customer_names.intersection(rel for _, rel in source_paths):
        raise PipelineContractError("A file cannot be both a source asset and a customer deliverable")

    provenance = [dict(item) for item in source_provenance]
    for item in provenance:
        if not isinstance(item.get("url"), str) or not item["url"].strip():
            raise PipelineContractError("Each source provenance record needs a URL")
        if not isinstance(item.get("license"), str) or not item["license"].strip():
            raise PipelineContractError("Each source provenance record needs a license")

    customer_evidence = [_file_evidence(path, rel) for path, rel in customer_paths]
    image_paths: list[tuple[Path, str]] = []
    image_names: set[str] = set()
    for raw in image_files:
        path, rel = _confined_file(root, raw, source="image")
        if not rel or Path(rel).parts[0].casefold() != "images":
            raise PipelineContractError(f"Listing image must remain under images/: {rel}")
        if rel.casefold() in image_names:
            raise PipelineContractError(f"Listing image path is duplicated: {rel}")
        _validate_listing_image(path, rel)
        image_names.add(rel.casefold())
        image_paths.append((path, rel))
    if image_paths and len(image_paths) != 10:
        raise PipelineContractError(f"Exactly 10 listing images are required when supplied, got {len(image_paths)}")
    manifest: dict[str, Any] = {
        "schema": "etsy.pipeline-product.v1",
        "contract_version": str(contract_version),
        "pipeline_job_id": manifest_job_id,
        "campaign_id": manifest_campaign_id,
        "candidate_id": manifest_candidate_id,
        "product_id": manifest_product_id,
        "shop_id": manifest_shop_id,
        "listing_type": "digital",
        "customer_files": [rel for _, rel in customer_paths],
        "customer_file_evidence": customer_evidence,
        "image_files": [rel for _, rel in image_paths],
        "image_file_evidence": [_file_evidence(path, rel) for path, rel in image_paths],
        "source_assets": [_file_evidence(path, rel) for path, rel in source_paths],
        "source_provenance": provenance,
        "source_policy": {"source_assets_separate": True, "customer_files_only": True},
    }
    if manifest_worker_id:
        manifest["worker_id"] = manifest_worker_id
    target = root / ".pipeline-product.json"
    if target.is_symlink():
        raise PipelineContractError("Pipeline manifest cannot be a symlink")
    fd, temp_name = tempfile.mkstemp(prefix=".pipeline-product.", suffix=".tmp", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return manifest


def read_pipeline_product_manifest(product_root: str | Path) -> dict[str, Any]:
    root_input = Path(product_root).expanduser()
    if root_input.is_symlink():
        raise PipelineContractError(f"Product root is a symlink: {root_input}")
    root = root_input.resolve()
    target = root / ".pipeline-product.json"
    if target.is_symlink() or not target.is_file():
        raise PipelineContractError("Pipeline product manifest is missing or unsafe")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineContractError("Pipeline product manifest is not valid JSON") from exc
    if value.get("schema") != "etsy.pipeline-product.v1" or value.get("listing_type") != "digital":
        raise PipelineContractError("Pipeline product manifest contract/listing_type is invalid")
    # Fail closed when a product has no durable pipeline identity.  Without
    # these fields the poster cannot prove which campaign/job owns the files,
    # so it must never treat the folder as a legacy listing.
    _manifest_identity(value.get("pipeline_job_id"), "pipeline_job_id")
    manifest_shop_id = _manifest_identity(value.get("shop_id"), "shop_id")
    if manifest_shop_id not in _PIPELINE_ALLOWED_SHOPS:
        raise PipelineContractError(f"Unsupported pipeline shop_id: {manifest_shop_id}")
    for field in ("campaign_id", "candidate_id", "product_id"):
        _manifest_identity(value.get(field), field)
    if "worker_id" in value:
        _manifest_identity(value.get("worker_id"), "worker_id", required=False)
    customer = value.get("customer_files")
    if not isinstance(customer, list) or not customer:
        raise PipelineContractError("Pipeline manifest customer_files must be a list")
    customer_paths = list(validate_customer_files([root / str(name) for name in customer]))
    if any(any(part.casefold() == "source_assets" for part in Path(str(name)).parts) for name in customer):
        raise PipelineContractError("Pipeline manifest exposes source_assets as customer file")
    evidence = value.get("customer_file_evidence")
    if not isinstance(evidence, list) or len(evidence) != len(customer_paths):
        raise PipelineContractError("Pipeline manifest customer_file_evidence is missing or incomplete")
    by_path = {str(item.get("path") or ""): item for item in evidence if isinstance(item, Mapping)}
    for raw_name, path in zip(customer, customer_paths):
        rel = path.relative_to(root).as_posix()
        record = by_path.get(rel)
        if record is None:
            raise PipelineContractError(f"Pipeline manifest has no evidence for customer file: {rel}")
        actual = _file_evidence(path, rel)
        if str(record.get("sha256") or "").lower() != actual["sha256"] or int(record.get("size") or -1) != actual["size"]:
            raise PipelineContractError(f"Customer artifact hash/size mismatch: {rel}")
        # Re-run the technical validator at read time, because the product may
        # have changed after the manifest was written.
        validate_final_artifact(path)

    source_records = value.get("source_assets", [])
    if not isinstance(source_records, list):
        raise PipelineContractError("Pipeline manifest source_assets must be a list")
    for record in source_records:
        if not isinstance(record, Mapping):
            raise PipelineContractError("Pipeline manifest source asset evidence is invalid")
        raw_path = str(record.get("path") or "")
        if not raw_path or any(part.casefold() == "source_assets" for part in Path(raw_path).parts) is False:
            raise PipelineContractError("Source asset evidence must remain under source_assets")
        source_path, rel = _confined_file(root, raw_path, source="source")
        actual = _file_evidence(source_path, rel)
        if str(record.get("sha256") or "").lower() != actual["sha256"] or int(record.get("size") or -1) != actual["size"]:
            raise PipelineContractError(f"Source asset hash/size mismatch: {rel}")
    image_names = value.get("image_files", [])
    image_records = value.get("image_file_evidence", [])
    if not isinstance(image_names, list) or not isinstance(image_records, list) or len(image_names) != len(image_records):
        raise PipelineContractError("Pipeline manifest image evidence is missing or incomplete")
    if image_names and len(image_names) != 10:
        raise PipelineContractError("Pipeline manifest must contain exactly 10 listing images")
    seen_images: set[str] = set()
    for raw_name, raw_record in zip(image_names, image_records):
        image_path, rel = _confined_file(root, str(raw_name), source="image")
        if not rel or Path(rel).parts[0].casefold() != "images":
            raise PipelineContractError(f"Pipeline manifest image must remain under images/: {rel}")
        if rel.casefold() in seen_images:
            raise PipelineContractError(f"Pipeline manifest image path is duplicated: {rel}")
        if not isinstance(raw_record, Mapping):
            raise PipelineContractError("Pipeline manifest image evidence is invalid")
        actual = _file_evidence(image_path, rel)
        _validate_listing_image(image_path, rel)
        if str(raw_record.get("path") or "") != rel or str(raw_record.get("sha256") or "").lower() != actual["sha256"] or int(raw_record.get("size") or -1) != actual["size"]:
            raise PipelineContractError(f"Listing image hash/size mismatch: {rel}")
        seen_images.add(rel.casefold())
    return value


__all__ = [
    "build_vector_delivery_zip",
    "read_pipeline_product_manifest",
    "write_pipeline_product_manifest",
]
