"""Core pipeline contracts and validators for the Etsy multi-shop flow."""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "PipelineContractError",
    "PipelinePolicy",
    "PipelineStage",
    "PipelineStatus",
    "ProductCandidate",
    "ArtifactEvidence",
    "OperationEnvelope",
    "VerificationReceipt",
    "source_fingerprint",
    "build_idempotency_key",
    "validate_customer_files",
    "validate_seo_payload",
    "validate_final_artifact",
]


class PipelineContractError(ValueError):
    """Raised when a contract check fails."""


class PipelineStage(str, Enum):
    RESEARCH = "research"
    DOWNLOAD = "download"
    ACQUIRE_SOURCE = "acquire-source"
    CREATE = "create"
    CREATE_PRODUCT = "create-product"
    IMAGE_GEN = "image_gen"
    GENERATE_IMAGES = "generate-images"
    SEO = "seo"
    SEO_PACKAGE = "seo-package"
    VALIDATE = "validate"
    VERIFY_ASSETS = "verify-assets"
    PREP_DRAFT = "prep_draft"
    CREATE_VERIFIED_DRAFT = "create-verified-draft"
    REVIEW = "review"
    DRAFT_APPROVAL = "draft-approval"
    PUBLISH = "publish"


class PipelineStatus(str, Enum):
    DRAFT = "draft"
    WAITING = "waiting"
    RUNNING = "running"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"
    VERIFIED = "verified"
    FAILED = "failed"


CONTRACT_VERSION = "1.0"
PDF_MAGIC = b"%PDF-"
SVG_EVENT_ATTR_RE = re.compile(r"^on[a-z0-9_-]+$", re.IGNORECASE)
SAFE_FILENAME_RE = re.compile(r"^[^\\/\x00]+$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


try:
    ASIA_HO_CHI_MINH = ZoneInfo("Asia/Ho_Chi_Minh")
except ZoneInfoNotFoundError:
    ASIA_HO_CHI_MINH = timezone.utc


@dataclass(frozen=True)
class PipelinePolicy:
    schedule_timezone: str = "Asia/Ho_Chi_Minh"
    max_drafts_per_day: int = 5
    max_drafts_per_shop_per_day: int = 2
    max_inflight_jobs: int = 10
    max_concurrent_chatgpt_jobs: int = 1
    listing_type: str = "digital"
    api_budget_usd: float = 0.0
    image_count: int = 10
    allowed_formats: tuple[str, ...] = ("pdf", "svg", "dxf", "zip")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_timezone": self.schedule_timezone,
            "max_drafts_per_day": self.max_drafts_per_day,
            "max_drafts_per_shop_per_day": self.max_drafts_per_shop_per_day,
            "max_inflight_jobs": self.max_inflight_jobs,
            "max_concurrent_chatgpt_jobs": self.max_concurrent_chatgpt_jobs,
            "listing_type": self.listing_type,
            "api_budget_usd": self.api_budget_usd,
            "image_count": self.image_count,
            "allowed_formats": list(self.allowed_formats),
        }


@dataclass(frozen=True)
class ProductCandidate:
    campaign_id: str
    shop_id: str
    product_id: str
    listing_type: str = "digital"
    created_at: datetime = field(default_factory=_utc_now)
    source_fingerprints: tuple[str, ...] = ()
    workflow_version: str = CONTRACT_VERSION
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactEvidence:
    artifact_id: str
    campaign_id: str
    shop_id: str
    product_id: str
    file_path: str
    file_sha256: str
    file_size_bytes: int
    format_hint: str
    created_at: datetime = field(default_factory=_utc_now)
    notes: dict[str, Any] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "campaign_id": self.campaign_id,
            "shop_id": self.shop_id,
            "product_id": self.product_id,
            "file_path": self.file_path,
            "file_sha256": self.file_sha256,
            "file_size_bytes": self.file_size_bytes,
            "format_hint": self.format_hint,
            "created_at": self.created_at.isoformat(),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class OperationEnvelope:
    operation_id: str
    contract_version: str
    campaign_id: str
    shop_id: str
    product_id: str
    stage: PipelineStage
    input_version: str
    source_checksum: str
    idempotency_key: str
    attempt_id: int = 1
    lock_owner: str | None = None
    lock_ttl_seconds: int = 1800
    heartbeat_at: datetime | None = None
    created_at: datetime = field(default_factory=_utc_now)

    def heartbeat_expired(self, now: datetime | None = None) -> bool:
        now = now or _utc_now()
        last_seen = self.heartbeat_at or self.created_at
        return (now - last_seen).total_seconds() > self.lock_ttl_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "contract_version": self.contract_version,
            "campaign_id": self.campaign_id,
            "shop_id": self.shop_id,
            "product_id": self.product_id,
            "stage": self.stage.value,
            "input_version": self.input_version,
            "source_checksum": self.source_checksum,
            "idempotency_key": self.idempotency_key,
            "attempt_id": self.attempt_id,
            "lock_owner": self.lock_owner,
            "lock_ttl_seconds": self.lock_ttl_seconds,
            "heartbeat_at": self.heartbeat_at.isoformat() if self.heartbeat_at else None,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class VerificationReceipt:
    receipt_id: str
    contract_version: str
    campaign_id: str
    shop_id: str
    product_id: str
    stage: PipelineStage
    status: PipelineStatus
    evidence: tuple[ArtifactEvidence, ...]
    manifest_path: str
    checksum: str
    created_at: datetime = field(default_factory=_utc_now)
    completed_at: datetime | None = None
    validation_errors: tuple[str, ...] = ()
    validation_warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "contract_version": self.contract_version,
            "campaign_id": self.campaign_id,
            "shop_id": self.shop_id,
            "product_id": self.product_id,
            "stage": self.stage.value,
            "status": self.status.value,
            "evidence": [item.to_manifest() for item in self.evidence],
            "manifest_path": self.manifest_path,
            "checksum": self.checksum,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "validation_errors": list(self.validation_errors),
            "validation_warnings": list(self.validation_warnings),
        }


def _safe_path(path: Path) -> Path:
    if not path.exists():
        raise PipelineContractError(f"Path not found: {path}")
    if not path.is_file():
        raise PipelineContractError(f"Path is not a file: {path}")

    name = path.name
    if not SAFE_FILENAME_RE.match(name):
        raise PipelineContractError(f"Unsafe file name: {name}")
    if name in {"", ".", ".."}:
        raise PipelineContractError(f"Unsafe file name: {name}")
    if path.suffix.lower() not in {".pdf", ".zip", ".svg", ".dxf"}:
        raise PipelineContractError(f"Unsupported file extension: {path.suffix}")
    return path


def _is_source_asset(path: Path) -> bool:
    # macOS commonly uses a case-insensitive filesystem; the contract must not
    # be bypassed with ``Source_Assets`` or another case variant.
    return any(str(part).casefold() == "source_assets" for part in path.parts)


def source_fingerprint(
    path: str | Path,
    *,
    chunk_size: int = 1024 * 1024,
    exclude_source_assets: bool = False,
) -> str:
    """Return a deterministic digest for a file or product tree.

    A product tree normally includes ``source_assets`` because those files are
    the provenance input that should invalidate a rerun when they change.  A
    caller inspecting only buyer-facing outputs can opt into excluding that
    directory explicitly.
    """
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise PipelineContractError(f"Path not found: {target}")
    if Path(path).expanduser().is_symlink():
        raise PipelineContractError(f"Symlink source path is not allowed: {path}")
    hasher = hashlib.sha256()

    if target.is_file():
        with target.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    if not target.is_dir():
        raise PipelineContractError(f"Unsupported source path type: {target}")

    for file in sorted(target.rglob("*"), key=lambda item: str(item.resolve())):
        if file.is_symlink():
            raise PipelineContractError(f"Symlink source file is not allowed: {file}")
        if not file.is_file():
            continue
        if exclude_source_assets and _is_source_asset(file):
            continue
        rel = str(file.relative_to(target).as_posix())
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\x00")
        with file.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                hasher.update(chunk)
        hasher.update(b"\x00")
    return hasher.hexdigest()


def build_idempotency_key(
    campaign_id: str,
    shop_id: str,
    product_id: str,
    stage: PipelineStage,
    *,
    input_version: str,
    source_fingerprint_value: str,
    contract_version: str = CONTRACT_VERSION,
) -> str:
    payload = {
        "contract_version": contract_version,
        "campaign_id": campaign_id,
        "shop_id": shop_id,
        "product_id": product_id,
        "stage": stage.value,
        "input_version": input_version,
        "source_fingerprint": source_fingerprint_value,
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _to_max_file_paths(files: Iterable[str | Path]) -> tuple[Path, ...]:
    if isinstance(files, (str, bytes, Path)):
        raise PipelineContractError("file_paths must be an iterable of paths, not one path string")
    normalized = []
    for raw in files:
        path = Path(raw).expanduser().resolve()
        normalized.append(path)
    return tuple(normalized)


def validate_customer_files(
    file_paths: Iterable[str | Path],
    *,
    max_files: int = 5,
    max_file_size_bytes: int = 20 * 1024 * 1024,
) -> tuple[Path, ...]:
    if not isinstance(file_paths, Iterable):
        raise PipelineContractError("file_paths must be iterable")
    normalized = _to_max_file_paths(file_paths)
    if len(normalized) == 0:
        raise PipelineContractError("No deliverable files provided")
    if len(normalized) > max_files:
        raise PipelineContractError(
            f"Too many deliverable files: {len(normalized)} (max {max_files})"
        )

    checked: list[Path] = []
    for path in normalized:
        if _is_source_asset(path):
            raise PipelineContractError(
                f"Source asset path is not allowed as customer file: {path}"
            )
        path = _safe_path(path)
        if path.suffix.lower() not in {".pdf", ".zip"}:
            raise PipelineContractError(
                f"Customer output must be PDF or ZIP, got {path.suffix}"
            )

        if path.stat().st_size > max_file_size_bytes:
            raise PipelineContractError(
                f"Customer file exceeds max size: {path.name} "
                f"({path.stat().st_size} > {max_file_size_bytes})"
            )
        checked.append(path)
    return tuple(checked)


def validate_seo_payload(
    *,
    title: str,
    tags: Iterable[str],
    description: str,
    evidence_urls: Iterable[str] | None = None,
) -> dict[str, Any]:
    normalized_title = (title or "").strip()
    if not normalized_title:
        raise PipelineContractError("Title is required")
    if len(normalized_title) > 140:
        raise PipelineContractError("Title must be <= 140 characters")

    normalized_tags: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = (raw or "").strip()
        if not tag:
            continue
        if len(tag) > 20:
            raise PipelineContractError("Each tag must be <= 20 characters")
        slug = re.sub(r"\s+", " ", tag.lower())
        if slug in seen:
            continue
        seen.add(slug)
        normalized_tags.append(tag.strip())

    if len(normalized_tags) != 13:
        raise PipelineContractError(f"Exactly 13 unique tags are required, got {len(normalized_tags)}")

    normalized_desc = (description or "").strip()
    if not normalized_desc:
        raise PipelineContractError("Description is required")
    if len(normalized_desc) < 20:
        raise PipelineContractError("Description is too short to be evidence-grounded")

    urls = list(evidence_urls or [])
    if urls:
        if not any(url in normalized_desc for url in urls):
            raise PipelineContractError("Description must reference at least one evidence URL")
    elif not re.search(r"https?://", normalized_desc):
        raise PipelineContractError("Description should include evidence links when possible")

    if "TODO" in normalized_desc.upper():
        raise PipelineContractError("Description appears to be a placeholder")

    return {
        "title": normalized_title,
        "tags": tuple(normalized_tags),
        "description": normalized_desc,
        "tag_count": len(normalized_tags),
    }


def validate_final_artifact(path: str | Path) -> ArtifactEvidence:
    original = Path(path).expanduser()
    if original.is_symlink():
        raise PipelineContractError(f"Symlink artifact is not allowed: {path}")
    target = original.resolve()
    if not target.exists():
        raise PipelineContractError(f"File not found: {target}")
    if not target.is_file():
        raise PipelineContractError(f"Path is not a file: {target}")
    if target.stat().st_size <= 0:
        raise PipelineContractError(f"Empty file is not a valid artifact: {target}")
    if target.name.endswith(".json"):
        raise PipelineContractError("Manifest-only artifacts must be passed as part of ZIP manifest handling")

    suffix = target.suffix.lower()
    if suffix == ".pdf":
        _validate_pdf(target)
    elif suffix == ".svg":
        _validate_svg(target)
    elif suffix == ".dxf":
        _validate_dxf(target)
    elif suffix == ".zip":
        _validate_zip(target)
    else:
        raise PipelineContractError(f"Unsupported artifact format: {target.suffix}")

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return ArtifactEvidence(
        artifact_id=f"artifact-{target.stem}",
        campaign_id="",
        shop_id="",
        product_id="",
        file_path=str(target),
        file_sha256=digest,
        file_size_bytes=target.stat().st_size,
        format_hint=suffix.lstrip("."),
    )


def _validate_pdf(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            head = handle.read(256)
    except OSError as exc:
        raise PipelineContractError(f"Failed to read PDF: {path}") from exc
    if not head.startswith(PDF_MAGIC):
        raise PipelineContractError(f"PDF header missing: {path}")
    if b"/Encrypt" in head:
        raise PipelineContractError(f"Encrypted PDF not allowed: {path}")
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise PipelineContractError(f"Failed to read PDF: {path}") from exc
    if b"/Encrypt" in body[:2 * 1024 * 1024]:
        raise PipelineContractError(f"Encrypted PDF not allowed: {path}")

    # PyMuPDF is part of the Etsy runtime requirements.  Opening every page
    # and asking for a small pixmap catches truncated cross-reference tables,
    # invalid page trees and pages that cannot render before the file reaches
    # Etsy.  Keep a structural fallback for constrained tooling that imports
    # the contract module without the optional runtime dependency.
    try:
        import pymupdf  # type: ignore
    except ImportError:
        if b"/Type /Pages" not in body and b"/Type /Page" not in body:
            raise PipelineContractError(f"PDF page tree could not be verified: {path}")
        return None

    document = None
    try:
        document = pymupdf.open(filename=str(path))
        page_count = int(document.page_count)
        if page_count <= 0:
            raise PipelineContractError(f"PDF has no pages: {path}")
        if page_count > 1000:
            raise PipelineContractError(f"PDF has too many pages for a listing artifact: {page_count}")
        matrix = pymupdf.Matrix(0.15, 0.15)
        for index in range(page_count):
            page = document.load_page(index)
            rect = page.rect
            if rect.width <= 0 or rect.height <= 0:
                raise PipelineContractError(f"PDF page {index + 1} has invalid dimensions: {path}")
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            if pixmap.width <= 0 or pixmap.height <= 0:
                raise PipelineContractError(f"PDF page {index + 1} did not render: {path}")
    except PipelineContractError:
        raise
    except Exception as exc:
        raise PipelineContractError(f"PDF cannot be opened/rendered: {path}") from exc
    finally:
        if document is not None:
            document.close()

def _validate_svg(path: Path) -> None:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw.lstrip(b"\xef\xbb\xbf")
    text = raw.decode("utf-8", errors="replace")
    if "<script" in text.lower():
        raise PipelineContractError(f"Embedded script not allowed in SVG: {path}")
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
        raise PipelineContractError(f"SVG should not contain external-safe hostile doctype/entity declarations: {path}")
    if re.search(r"url\([^\)]*(https?|data):", text, re.IGNORECASE):
        raise PipelineContractError(f"SVG contains external references: {path}")

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise PipelineContractError(f"Invalid SVG XML: {path}") from exc

    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise PipelineContractError(f"Root element must be svg: {path}")
    has_view_box = "viewBox" in root.attrib
    has_size = "width" in root.attrib and "height" in root.attrib
    if not has_view_box and not has_size:
        raise PipelineContractError(f"SVG missing viewBox or width/height: {path}")

    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag == "script":
            raise PipelineContractError(f"Script tag not allowed in SVG: {path}")
        for attr in element.attrib:
            attr_name = attr.rsplit(":", 1)[-1].lower()
            if SVG_EVENT_ATTR_RE.match(attr_name):
                raise PipelineContractError(f"SVG event attribute '{attr}' is not allowed: {path}")
            value = element.attrib[attr].strip().lower()
            if attr_name in {"href", "xlink:href"} and (
                value.startswith(("javascript:", "http://", "https://", "data:", "file:", "//"))
            ):
                raise PipelineContractError(f"SVG external href not allowed: {path}")

    # A usable vector needs positive numeric dimensions/viewBox and at least
    # one drawable element. This catches placeholder XML and malformed units
    # before an image worker or Etsy receives it.
    def _positive_number(raw: str, label: str) -> float:
        match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:px|pt|mm|cm|in)?\s*", raw or "")
        if not match:
            raise PipelineContractError(f"SVG {label} must be a finite positive number: {path}")
        value = float(match.group(1))
        if not math.isfinite(value) or value <= 0:
            raise PipelineContractError(f"SVG {label} must be positive: {path}")
        return value

    if has_size:
        _positive_number(root.attrib.get("width", ""), "width")
        _positive_number(root.attrib.get("height", ""), "height")
    if has_view_box:
        parts = re.split(r"[\s,]+", root.attrib.get("viewBox", "").strip())
        if len(parts) != 4:
            raise PipelineContractError(f"SVG viewBox must contain four numbers: {path}")
        try:
            _, _, vb_width, vb_height = (float(item) for item in parts)
        except ValueError as exc:
            raise PipelineContractError(f"SVG viewBox must be numeric: {path}") from exc
        if not all(math.isfinite(item) for item in (vb_width, vb_height)) or vb_width <= 0 or vb_height <= 0:
            raise PipelineContractError(f"SVG viewBox must have positive dimensions: {path}")
    drawable = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "image", "g"}
    if not any(element.tag.rsplit("}", 1)[-1].lower() in drawable for element in root.iter() if element is not root):
        raise PipelineContractError(f"SVG has no drawable vector content: {path}")


def _validate_dxf(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PipelineContractError(f"Failed to read DXF: {path}") from exc

    lines = text.splitlines()
    if len(lines) < 4 or len(lines) % 2:
        raise PipelineContractError(f"DXF group-code pairs are malformed: {path}")
    pairs: list[tuple[int, str]] = []
    for index in range(0, len(lines), 2):
        raw_code = lines[index].strip()
        try:
            code = int(raw_code)
        except ValueError as exc:
            raise PipelineContractError(f"DXF group code is not numeric at line {index + 1}: {path}") from exc
        if code < 0:
            raise PipelineContractError(f"DXF group code is invalid: {path}")
        pairs.append((code, lines[index + 1].strip()))
    upper = text.upper()
    if "3DFACE" in upper or "3DSOLID" in upper or "3DLINE" in upper or "ACIS" in upper or "POLYFACE" in upper:
        raise PipelineContractError(f"DXF appears to contain 3D geometry: {path}")
    section_names = [value.upper() for code, value in pairs if code == 2]
    if "ENTITIES" not in section_names or not any(code == 0 and value.upper() == "SECTION" for code, value in pairs):
        raise PipelineContractError(f"DXF missing ENTITIES section: {path}")
    if not any(code == 0 and value.upper() == "ENDSEC" for code, value in pairs):
        raise PipelineContractError(f"DXF missing ENDSEC: {path}")
    if not any(code == 0 and value.upper() == "EOF" for code, value in pairs):
        raise PipelineContractError(f"DXF missing EOF: {path}")
    for index, (code, value) in enumerate(pairs):
        if code == 9 and value.upper() == "$INSUNITS":
            unit_pair = next((item for item in pairs[index + 1 :] if item[0] == 70), None)
            if unit_pair is None:
                raise PipelineContractError(f"DXF $INSUNITS header is missing: {path}")
            try:
                unit_code = int(unit_pair[1])
            except ValueError as exc:
                raise PipelineContractError(f"DXF $INSUNITS is not numeric: {path}") from exc
            if unit_code < 0 or unit_code > 20:
                raise PipelineContractError(f"DXF $INSUNITS is unsupported: {unit_code}: {path}")
            break
    allowed_entities = {"POINT", "LINE", "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE", "POLYLINE", "SPLINE"}
    in_entities = False
    entity_count = 0
    for code, value in pairs:
        upper_value = value.upper()
        if code == 0 and upper_value == "SECTION":
            in_entities = False
            continue
        if code == 2 and upper_value == "ENTITIES":
            in_entities = True
            continue
        if code == 0 and upper_value == "ENDSEC":
            in_entities = False
            continue
        if in_entities and code == 0 and upper_value not in {"SECTION", "ENDSEC"}:
            if upper_value not in allowed_entities:
                raise PipelineContractError(f"DXF entity is unsupported or 3D: {value}: {path}")
            entity_count += 1
        if in_entities and code in {10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39}:
            try:
                numeric = float(value)
            except ValueError as exc:
                raise PipelineContractError(f"DXF coordinate is not numeric: {path}") from exc
            if not math.isfinite(numeric):
                raise PipelineContractError(f"DXF coordinate is not finite: {path}")
            if abs(numeric) > 1e9:
                raise PipelineContractError(f"DXF coordinate is outside finite bounds: {path}")
            if code in {30, 31, 32, 33, 34, 35, 36, 37, 38, 39} and abs(numeric) > 1e-9:
                raise PipelineContractError(f"DXF contains non-zero Z/elevation geometry: {path}")
    if entity_count == 0:
        raise PipelineContractError(f"DXF ENTITIES section is empty: {path}")


def _validate_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            if not names:
                raise PipelineContractError(f"ZIP must contain at least one member: {path}")
            total_uncompressed = 0
            has_primary = False
            primary_members: list[tuple[zipfile.ZipInfo, str]] = []
            seen_member_names: set[str] = set()
            allowed_info_names = {
                "manifest.json",
                "readme",
                "readme.txt",
                "readme.md",
                "license",
                "license.txt",
                "license.md",
            }
            for info in zf.infolist():
                member_name = info.filename.replace("\\", "/")
                if member_name in {"", "."}:
                    raise PipelineContractError(f"Invalid ZIP member name: {member_name}")
                if member_name.startswith("/") or member_name.startswith("../") or "/../" in member_name:
                    raise PipelineContractError(f"ZIP member traversal detected: {member_name}")
                if any(part.casefold() == "source_assets" for part in member_name.split("/")):
                    raise PipelineContractError(f"ZIP cannot contain source_assets: {member_name}")
                # Buyer archives are deliberately flat.  Besides simplifying
                # Etsy's upload/readback path, this blocks a source folder or
                # an unexpected nested payload from hiding behind a valid
                # primary file.
                if len(Path(member_name).parts) != 1:
                    raise PipelineContractError(f"ZIP member must be top-level: {member_name}")
                member_key = member_name.casefold()
                if member_key in seen_member_names:
                    raise PipelineContractError(f"ZIP contains duplicate member names: {member_name}")
                seen_member_names.add(member_key)
                if info.is_dir():
                    continue

                # symlink check
                unix_mode = (info.external_attr >> 16) & 0o777777
                if stat.S_ISLNK(unix_mode):
                    raise PipelineContractError(f"ZIP member is a symlink: {member_name}")

                total_uncompressed += int(info.file_size)
                if info.file_size > 20 * 1024 * 1024:
                    raise PipelineContractError(
                        f"ZIP member exceeds limit (20MB): {member_name}"
                    )

                lower = member_name.casefold()
                basename = Path(member_name).name.casefold()
                if not (
                    basename in allowed_info_names
                    or Path(member_name).suffix.casefold() in {".pdf", ".svg", ".dxf"}
                ):
                    raise PipelineContractError(
                        "ZIP member is not an allowed buyer artifact or manifest/readme/license: "
                        f"{member_name}"
                    )
                if lower.endswith(".pdf") or lower.endswith(".svg") or lower.endswith(".dxf"):
                    has_primary = True
                    primary_members.append((info, member_name))
                if lower.endswith(".zip"):
                    raise PipelineContractError(f"Nested ZIP is not allowed in buyer delivery: {member_name}")

            if total_uncompressed > 200 * 1024 * 1024:
                raise PipelineContractError(
                    f"ZIP uncompressed payload exceeds limit (200MB): {total_uncompressed}"
                )
            if not has_primary:
                raise PipelineContractError("ZIP must include at least one PDF/SVG/DXF file")

            # Validate every contained buyer artifact, rather than accepting a
            # ZIP whose top-level container is valid but whose PDF/SVG/DXF is
            # corrupt or carries unsafe references.
            with tempfile.TemporaryDirectory(prefix="etsy-artifact-zip-") as tmp:
                root = Path(tmp)
                for info, member_name in primary_members:
                    target = root / Path(member_name).name
                    target.write_bytes(zf.read(info))
                    if target.stat().st_size != info.file_size:
                        raise PipelineContractError(f"ZIP member size changed while validating: {member_name}")
                    suffix = target.suffix.lower()
                    if suffix == ".pdf":
                        _validate_pdf(target)
                    elif suffix == ".svg":
                        _validate_svg(target)
                    elif suffix == ".dxf":
                        _validate_dxf(target)

            safe_suffix = ".zip"
            if not path.name.lower().endswith(safe_suffix):
                raise PipelineContractError("ZIP artifact must be a .zip file")
    except zipfile.BadZipFile as exc:
        raise PipelineContractError(f"Invalid ZIP archive: {path}") from exc
