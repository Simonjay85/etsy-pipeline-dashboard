#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

IMAGE_SUFFIXES = {".apng", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp", ".tiff", ".tif"}
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class AssetReadinessError(RuntimeError):
    """Raised when one or more selected assets are not safe for upload/update."""

    def __init__(self, report: "AssetReadinessReport") -> None:
        self.report = report
        super().__init__(report.format_blocked_summary())


@dataclass(frozen=True)
class AssetReadinessItem:
    path: str
    status: str
    reason: str
    remediation: str
    expected_sha256: str | None = None
    actual_sha256: str | None = None


@dataclass(frozen=True)
class AssetReadinessReport:
    """Deterministic readiness report for a selected asset set."""

    items: tuple[AssetReadinessItem, ...]

    @property
    def is_blocked(self) -> bool:
        return not self.is_ready

    @property
    def is_ready(self) -> bool:
        return all(item.status == "ready" for item in self.items)

    @property
    def blocked_items(self) -> tuple[AssetReadinessItem, ...]:
        return tuple(item for item in self.items if item.status != "ready")

    def format_blocked_summary(self) -> str:
        blocked = [f"{item.path}: {item.reason}" for item in self.blocked_items]
        if not blocked:
            return "all assets are ready"
        return "asset readiness blocked: " + "; ".join(blocked)


@dataclass(frozen=True)
class _AssetSpec:
    path: Path
    expected_sha256: str | None = None
    expect_cloud_only: bool = False


class AssetReadinessEngine:
    """Deterministic preflight for selected local/cloud-aware assets."""

    def __init__(self, *, image_suffixes: set[str] | None = None) -> None:
        self._image_suffixes = {ext.lower() for ext in (image_suffixes or IMAGE_SUFFIXES)}

    def evaluate(
        self,
        selected: Sequence[str | Path | Mapping[str, object]],
        *,
        expected_sha256_by_path: Mapping[str, str] | None = None,
        cloud_only: Iterable[str | Path] | None = None,
        raise_on_blocked: bool = True,
    ) -> AssetReadinessReport:
        cloud_only_index = _build_cloud_only_index(cloud_only or ())
        expected_map = expected_sha256_by_path or {}
        items: list[AssetReadinessItem] = []

        for raw in selected:
            spec = _coerce_spec(raw, expected_map)
            report_item = self._classify(spec, cloud_only_index)
            items.append(report_item)

        result = AssetReadinessReport(items=tuple(items))
        if raise_on_blocked and result.is_blocked:
            raise AssetReadinessError(result)
        return result

    def assert_ready(
        self,
        selected: Sequence[str | Path | Mapping[str, object]],
        *,
        expected_sha256_by_path: Mapping[str, str] | None = None,
        cloud_only: Iterable[str | Path] | None = None,
    ) -> AssetReadinessReport:
        return self.evaluate(
            selected,
            expected_sha256_by_path=expected_sha256_by_path,
            cloud_only=cloud_only,
            raise_on_blocked=True,
        )

    def _classify(
        self,
        spec: _AssetSpec,
        cloud_only_index: Mapping[str, str],
    ) -> AssetReadinessItem:
        path = spec.path
        path_key = str(path)
        path_str = str(path)
        norm_key = _normalized_path_key(path)

        if spec.expect_cloud_only:
            return AssetReadinessItem(
                path=path_str,
                status="cloud-only",
                reason="asset is marked as cloud-only explicitly",
                remediation="hydrate or restore this asset from cloud before upload",
            )

        if not path.exists():
            if path_key in cloud_only_index or norm_key in cloud_only_index:
                return AssetReadinessItem(
                    path=path_str,
                    status="cloud-only",
                    reason="asset exists only in cloud cache",
                    remediation="hydrate or restore this asset from cloud before upload",
                )
            return AssetReadinessItem(
                path=path_str,
                status="missing",
                reason="asset file does not exist",
                remediation="restore/download the file into the selected product folder",
            )

        try:
            info = path.lstat()
        except OSError as exc:
            return AssetReadinessItem(
                path=path_str,
                status="missing",
                reason=f"asset file cannot be statted: {exc}",
                remediation="restore/download the file into the selected product folder",
            )

        if not stat_module.S_ISREG(info.st_mode):
            return AssetReadinessItem(
                path=path_str,
                status="corrupt",
                reason="asset path is not a regular file",
                remediation="replace this asset with a valid file",
            )

        if info.st_size == 0:
            return AssetReadinessItem(
                path=path_str,
                status="zero-byte",
                reason="asset is zero-byte",
                remediation="re-export or restore binary content for this file",
            )

        if is_dataless(path, info=info):
            return AssetReadinessItem(
                path=path_str,
                status="dataless",
                reason="asset is an iCloud dataless placeholder",
                remediation="hydrate this placeholder in Finder/iCloud before upload",
            )

        if path.suffix.lower() in self._image_suffixes:
            decode_ok, decode_error = verify_image(path)
            if not decode_ok:
                return AssetReadinessItem(
                    path=path_str,
                    status="corrupt",
                    reason=f"cannot decode image: {decode_error}",
                    remediation="replace this image with a valid asset file",
                )

        expected_sha = spec.expected_sha256
        actual_sha = sha256_for_file(path)
        if expected_sha is not None and expected_sha != actual_sha:
            return AssetReadinessItem(
                path=path_str,
                status="checksum-mismatch",
                reason="asset checksum does not match expected SHA-256",
                remediation="rebuild this file from source or update the expected checksum",
                expected_sha256=expected_sha,
                actual_sha256=actual_sha,
            )

        return AssetReadinessItem(
            path=path_str,
            status="ready",
            reason="asset is ready",
            remediation="no action required",
            expected_sha256=expected_sha,
            actual_sha256=actual_sha,
        )


def _coerce_expected_sha(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TypeError(f"expected sha256 must be a hex string, got {type(raw)!r}")

    raw = raw.strip().lower()
    if not raw:
        return None
    if not _SHA256_RE.fullmatch(raw):
        raise ValueError(f"invalid expected sha256: {raw!r}")
    return raw


def _normalized_path_key(path: Path) -> str:
    try:
        return str(path.absolute())
    except Exception:
        return str(path)


def _coerce_spec(
    raw: str | Path | Mapping[str, object],
    expected_sha256_by_path: Mapping[str, str],
) -> _AssetSpec:
    if isinstance(raw, Mapping):
        raw_path = raw.get("path")
        if raw_path is None:
            raise ValueError("asset spec requires 'path'")
        path = Path(str(raw_path))
        expect_cloud_only = bool(raw.get("cloud_only") or raw.get("cloudOnly") or raw.get("state") == "cloud-only")
        expected_sha = _coerce_expected_sha(raw.get("expected_sha256") or raw.get("sha256"))
        return _AssetSpec(path=path, expected_sha256=expected_sha, expect_cloud_only=expect_cloud_only)

    path = Path(raw)
    return _AssetSpec(
        path=path,
        expected_sha256=_lookup_expected_sha(path, expected_sha256_by_path),
    )


def _build_cloud_only_index(cloud_only: Iterable[str | Path]) -> dict[str, str]:
    index: dict[str, str] = {}
    for item in cloud_only:
        path = Path(item)
        normalized = _normalized_path_key(path)
        index[path.name] = str(path)
        index[normalized] = str(path)
        index[str(path)] = str(path)
    return index


def _lookup_expected_sha(
    path: Path,
    expected_sha256_by_path: Mapping[str, str],
) -> str | None:
    candidates = {
        str(path),
        _normalized_path_key(path),
        path.name,
    }
    for candidate in candidates:
        if candidate in expected_sha256_by_path:
            return _coerce_expected_sha(expected_sha256_by_path[candidate])
    return None


def sha256_for_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def is_dataless(path: Path, info: os.stat_result | None = None) -> bool:
    """Detect iCloud placeholders without hydrating content."""

    info = info or path.lstat()
    if info.st_size <= 0:
        return False
    if getattr(info, "st_blocks", None) == 0:
        return True
    if sys.platform != "darwin":
        return False
    stat_bin = Path("/usr/bin/stat")
    if not stat_bin.exists():
        return False
    result = subprocess.run(
        [str(stat_bin), "-f", "%Sf", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "dataless" in result.stdout


def verify_image(path: Path) -> tuple[bool, str | None]:
    """Return (ok, error) for image decode validation."""

    try:
        from PIL import Image  # type: ignore
    except Exception:
        Image = None

    if Image is not None:
        try:
            with Image.open(path) as image:
                image.verify()
            return True, None
        except Exception as exc:  # pragma: no cover - backend dependent
            return False, str(exc)

    try:
        header = path.read_bytes(16)
    except OSError as exc:
        return False, str(exc)

    if len(header) < 8:
        return False, "image file is too small"

    # PNG/APNG
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return True, None

    # JPEG/JFIF
    if header.startswith(b"\xff\xd8"):
        return True, None

    # GIF
    if header.startswith((b"GIF87a", b"GIF89a")):
        return True, None

    # BMP
    if header.startswith(b"BM"):
        return True, None

    # TIFF
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return True, None

    # WEBP
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return True, None

    return False, "unknown/corrupt image format"


def classify_assets(
    selected: Sequence[str | Path | Mapping[str, object]],
    *,
    expected_sha256_by_path: Mapping[str, str] | None = None,
    cloud_only: Iterable[str | Path] | None = None,
    raise_on_blocked: bool = True,
) -> AssetReadinessReport:
    """Convenience wrapper around :class:`AssetReadinessEngine`."""

    return AssetReadinessEngine().evaluate(
        selected,
        expected_sha256_by_path=expected_sha256_by_path,
        cloud_only=cloud_only,
        raise_on_blocked=raise_on_blocked,
    )
