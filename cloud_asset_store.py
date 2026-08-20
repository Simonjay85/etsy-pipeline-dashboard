"""Immutable Google Drive-backed asset storage for Etsy products.

The store exposes a small, injectable API for immutable upload/verification
and Phase 2A browser/post hydration.  Dashboard and sync/factory integration
remain outside this module so callers can consume verified paths without
changing the on-disk or remote contract.

Remote OAuth credentials are owned by rclone.  This module only receives a
remote name and a Drive parent folder ID; it never reads or prints rclone's
configuration or credentials.
"""

from __future__ import annotations

import contextlib
import datetime as datetime_module
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from cloud_asset_store_config import (
    DEFAULT_CACHE_RELATIVE,
    DEFAULT_FAILURE_TTL_SECONDS,
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    DEFAULT_OFFLOAD_AGE_DAYS,
    DEFAULT_PARENT_ID,
    DEFAULT_REMOTE,
    DEFAULT_RCLONE_BIN,
    DEFAULT_SUCCESS_TTL_SECONDS,
)


SCHEMA_VERSION = 1
STATE_FILE_NAME = ".cloud-assets.json"
LOCK_FILE_NAME = ".cloud-assets.lock"
PREVIEW_FILE_NAME = ".cloud-preview.webp"
CONTENT_DIRS = ("images", "files")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
HYDRATION_METADATA_DIR_NAME = "hydration-metadata"
HYDRATION_LOCK_DIR_NAME = "hydration-locks"
DEFAULT_HYDRATION_PURPOSE = "asset-consumer"
REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REMOTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DRIVE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{5,256}$")

STATES = (
    "LOCAL_ONLY",
    "UPLOADING",
    "CLOUD_VERIFIED",
    "RESTORE_VERIFIED",
    "OFFLOAD_SCHEDULED",
    "CLOUD_ONLY",
    "RESTORING",
    "READY_LOCAL",
    "DIRTY_LOCAL",
    "CLEANUP_PENDING",
    "ERROR",
)

_STATE_TRANSITIONS = {
    "LOCAL_ONLY": {
        "LOCAL_ONLY",
        "UPLOADING",
        "ERROR",
        "DIRTY_LOCAL",
        "CLOUD_ONLY",
        "OFFLOAD_SCHEDULED",
    },
    "UPLOADING": {"UPLOADING", "CLOUD_VERIFIED", "ERROR"},
    "CLOUD_VERIFIED": {
        "CLOUD_VERIFIED",
        "UPLOADING",
        "RESTORING",
        "RESTORE_VERIFIED",
        "OFFLOAD_SCHEDULED",
        "CLOUD_ONLY",
        "DIRTY_LOCAL",
        "ERROR",
    },
    "RESTORE_VERIFIED": {"RESTORE_VERIFIED", "READY_LOCAL", "CLOUD_ONLY", "ERROR"},
    "OFFLOAD_SCHEDULED": {
        "OFFLOAD_SCHEDULED",
        "CLOUD_VERIFIED",
        "RESTORE_VERIFIED",
        "CLOUD_ONLY",
        "RESTORING",
        "UPLOADING",
        "READY_LOCAL",
        "DIRTY_LOCAL",
        "ERROR",
    },
    "CLOUD_ONLY": {
        "CLOUD_ONLY",
        "RESTORING",
        "DIRTY_LOCAL",
        "CLOUD_VERIFIED",
        "OFFLOAD_SCHEDULED",
        "CLEANUP_PENDING",
        "ERROR",
    },
    "RESTORING": {"RESTORING", "RESTORE_VERIFIED", "READY_LOCAL", "ERROR"},
    "READY_LOCAL": {
        "READY_LOCAL",
        "CLOUD_VERIFIED",
        "UPLOADING",
        "RESTORING",
        "RESTORE_VERIFIED",
        "OFFLOAD_SCHEDULED",
        "DIRTY_LOCAL",
        "ERROR",
    },
    "DIRTY_LOCAL": {
        "DIRTY_LOCAL",
        "UPLOADING",
        "RESTORING",
        "CLOUD_VERIFIED",
        "OFFLOAD_SCHEDULED",
        "CLOUD_ONLY",
        "ERROR",
    },
    "CLEANUP_PENDING": {
        "CLEANUP_PENDING",
        "CLOUD_ONLY",
        "RESTORING",
        "ERROR",
    },
    "ERROR": {
        "ERROR",
        "LOCAL_ONLY",
        "UPLOADING",
        "RESTORING",
        "DIRTY_LOCAL",
        "CLOUD_ONLY",
        "CLOUD_VERIFIED",
        "OFFLOAD_SCHEDULED",
    },
}


class CloudAssetError(RuntimeError):
    """Base error for safe, user-facing cloud asset failures."""


class AssetValidationError(CloudAssetError):
    """Raised when a product is unsafe or incomplete to store."""


class RemoteStoreError(CloudAssetError):
    """Raised for remote upload, verification, or restore failures."""


class RemoteConflictError(RemoteStoreError):
    """Raised when an immutable revision already exists."""


def utc_now() -> datetime_module.datetime:
    return datetime_module.datetime.now(datetime_module.timezone.utc)


def utc_text(value: Optional[datetime_module.datetime] = None) -> str:
    timestamp = value or utc_now()
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=datetime_module.timezone.utc)
    return timestamp.astimezone(datetime_module.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> Optional[datetime_module.datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime_module.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime_module.timezone.utc)
    return parsed.astimezone(datetime_module.timezone.utc)


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact_error(value: str) -> str:
    """Keep subprocess errors useful without echoing credential-like values."""

    redacted = re.sub(
        r"(?i)(access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization)\s*[=:]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        value,
    )
    return redacted[:1000]


def _safe_component(value: str, label: str) -> str:
    text = str(value).strip()
    if not text or text in {".", ".."} or "\x00" in text:
        raise AssetValidationError(f"invalid {label}: {value!r}")
    if "/" in text or "\\" in text or Path(text).is_absolute():
        raise AssetValidationError(f"path traversal is not allowed for {label}: {value!r}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", text):
        raise AssetValidationError(f"unsafe {label}: {value!r}")
    return text


def _safe_relative(value: str) -> str:
    text = str(value).replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or "\x00" in text:
        raise AssetValidationError(f"unsafe relative asset path: {value!r}")
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise AssetValidationError(f"path traversal is not allowed: {value!r}")
    return "/".join(_safe_asset_component(part) for part in parts)


def _safe_asset_component(value: str) -> str:
    """Validate a filename without imposing product-ID naming rules."""

    text = str(value)
    if not text or text in {".", ".."} or "\x00" in text:
        raise AssetValidationError(f"unsafe asset filename: {value!r}")
    if any(ord(character) < 32 for character in text):
        raise AssetValidationError(f"control character is not allowed in asset filename: {value!r}")
    if "/" in text or "\\" in text:
        raise AssetValidationError(f"path traversal is not allowed in asset filename: {value!r}")
    return text


def _safe_remote_relative(value: str) -> str:
    text = _safe_relative(value)
    if text.startswith("assets/v1/"):
        return text
    raise RemoteStoreError(f"remote path must stay under assets/v1/: {value!r}")


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError as exc:
        raise AssetValidationError(f"missing asset: {path}") from exc


def _is_dataless(path: Path, info: Optional[os.stat_result] = None) -> bool:
    """Detect iCloud placeholders without hydrating them.

    macOS reports these files as ``compressed,dataless``.  The block count
    catches the same condition without invoking a filesystem helper, which
    keeps tests deterministic and avoids reading placeholder contents.
    """

    info = info or _lstat(path)
    if getattr(info, "st_blocks", None) == 0 and info.st_size > 0:
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


def _validate_regular_file(path: Path) -> os.stat_result:
    info = _lstat(path)
    if stat_module.S_ISLNK(info.st_mode):
        raise AssetValidationError(f"symlinks are not allowed: {path}")
    if not stat_module.S_ISREG(info.st_mode):
        raise AssetValidationError(f"asset is not a regular file: {path}")
    if info.st_size <= 0:
        raise AssetValidationError(f"zero-byte asset is not allowed: {path}")
    if _is_dataless(path, info):
        raise AssetValidationError(f"iCloud dataless placeholder must be hydrated: {path}")
    return info


def _ensure_directory(path: Path, label: str) -> None:
    info = _lstat(path)
    if stat_module.S_ISLNK(info.st_mode):
        raise AssetValidationError(f"symlinks are not allowed for {label}: {path}")
    if not stat_module.S_ISDIR(info.st_mode):
        raise AssetValidationError(f"{label} is not a directory: {path}")


@dataclass(frozen=True)
class ProductIdentity:
    """The canonical local and remote identity of one product."""

    scope: str
    product: str
    shop: Optional[str] = None

    def __post_init__(self) -> None:
        if self.scope not in {"shops", "master_products"}:
            raise AssetValidationError(f"unsupported product scope: {self.scope}")
        _safe_component(self.product, "product")
        if self.scope == "shops":
            if not self.shop:
                raise AssetValidationError("shop is required for shop products")
            _safe_component(self.shop, "shop")
        elif self.shop is not None:
            raise AssetValidationError("master products cannot have a shop")

    @property
    def key(self) -> str:
        if self.scope == "shops":
            return f"shops/{self.shop}/{self.product}"
        return f"master_products/{self.product}"

    @property
    def remote_prefix(self) -> str:
        return f"assets/v1/{self.key}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "shop": self.shop,
            "product": self.product,
            "key": self.key,
        }


def resolve_product(repo_root: Path, product_root: Union[str, Path]) -> Tuple[Path, ProductIdentity]:
    """Resolve a product only inside the canonical repository layout."""

    root = Path(repo_root).expanduser().absolute()
    raw_candidate = Path(product_root).expanduser()
    if ".." in raw_candidate.parts:
        raise AssetValidationError(f"path traversal is not allowed for product: {product_root!r}")
    candidate = raw_candidate
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(str(candidate)))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise AssetValidationError(f"product is outside repository root: {candidate}") from exc

    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AssetValidationError(f"symlinks are not allowed in product path: {current}")

    parts = relative.parts
    if len(parts) == 2 and parts[0] == "master_products":
        identity = ProductIdentity("master_products", _safe_component(parts[1], "product"))
    elif len(parts) == 3 and parts[0] == "shops":
        identity = ProductIdentity(
            "shops",
            _safe_component(parts[2], "product"),
            _safe_component(parts[1], "shop"),
        )
    else:
        raise AssetValidationError(
            "product path must be master_products/<product> or shops/<shop>/<product>"
        )
    _ensure_directory(candidate, "product root")
    return candidate, identity


@dataclass(frozen=True)
class AssetRecord:
    path: str
    role: str
    size: int
    sha256: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "size": self.size,
            "sha256": self.sha256,
        }


def _collect_records(product_root: Path) -> List[AssetRecord]:
    records: List[AssetRecord] = []
    for dirname, role in (("images", "image"), ("files", "file")):
        directory = product_root / dirname
        _ensure_directory(directory, dirname)
        found = False
        for current, dirnames, filenames in os.walk(directory, topdown=True, followlinks=False):
            current_path = Path(current)
            safe_dirnames = []
            for name in sorted(dirnames):
                child = current_path / name
                info = _lstat(child)
                if stat_module.S_ISLNK(info.st_mode):
                    raise AssetValidationError(f"symlinks are not allowed: {child}")
                if not stat_module.S_ISDIR(info.st_mode):
                    raise AssetValidationError(f"asset directory entry is not a directory: {child}")
                safe_dirnames.append(name)
            dirnames[:] = safe_dirnames
            for name in sorted(filenames):
                if name == ".DS_Store":
                    continue
                path = current_path / name
                info = _validate_regular_file(path)
                relative = _safe_relative(path.relative_to(product_root).as_posix())
                records.append(AssetRecord(relative, role, int(info.st_size), sha256_file(path)))
                found = True
        if not found:
            raise AssetValidationError(f"missing usable {dirname} assets: {directory}")

    preview = product_root / PREVIEW_FILE_NAME
    if preview.exists() or preview.is_symlink():
        info = _validate_regular_file(preview)
        records.append(AssetRecord(PREVIEW_FILE_NAME, "preview", int(info.st_size), sha256_file(preview)))
    return sorted(records, key=lambda item: (item.role, item.path))


def _ensure_cloud_preview(product_root: Path) -> bool:
    """Best-effort creation of the small dashboard preview marker.

    The preview is deliberately optional in the manifest: a missing image
    conversion tool must never turn a valid asset upload into a destructive or
    otherwise unsafe operation.  When Pillow or the macOS ``cwebp`` utility is
    available, the first deterministic image is converted to a bounded WebP
    beside the product folder.  The full source images remain untouched.
    """

    destination = product_root / PREVIEW_FILE_NAME
    if destination.exists() or destination.is_symlink():
        _validate_regular_file(destination)
        return False

    image_source: Optional[Path] = None
    image_dir = product_root / "images"
    if image_dir.is_symlink() or not image_dir.is_dir():
        return False
    for current, dirnames, filenames in os.walk(image_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_dirnames: List[str] = []
        for name in sorted(dirnames):
            child = current_path / name
            if child.is_symlink():
                raise AssetValidationError(f"symlinks are not allowed: {child}")
            safe_dirnames.append(name)
        dirnames[:] = safe_dirnames
        for name in sorted(filenames):
            if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            candidate = current_path / name
            _validate_regular_file(candidate)
            image_source = candidate
            break
        if image_source is not None:
            break
    if image_source is None:
        return False

    temporary_name: Optional[str] = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=".cloud-preview-",
            suffix=".webp",
            dir=str(product_root.parent),
        )
        os.close(fd)
        temporary = Path(temporary_name)

        converted = False
        try:
            from PIL import Image  # type: ignore

            with Image.open(image_source) as image:
                image.thumbnail((320, 320))
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA")
                image.save(temporary, format="WEBP", quality=78, method=6)
            converted = True
        except Exception:
            # Pillow is an optional runtime dependency for the cloud layer.
            # Fall through to cwebp on macOS rather than failing an upload.
            converted = False

        if not converted:
            cwebp = shutil.which("cwebp")
            if not cwebp:
                return False
            try:
                subprocess.run(
                    [cwebp, "-quiet", "-resize", "320", "320", str(image_source), "-o", str(temporary)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                )
                converted = True
            except (OSError, subprocess.SubprocessError):
                converted = False

        if not converted:
            return False
        _validate_regular_file(temporary)
        os.replace(temporary, destination)
        return True
    except (AssetValidationError, OSError, ValueError, TypeError):
        return False
    finally:
        if temporary_name:
            temporary_path = Path(temporary_name)
            if temporary_path.exists() or temporary_path.is_symlink():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass


def _counts(records: Sequence[AssetRecord]) -> Dict[str, int]:
    images = [record for record in records if record.role == "image"]
    files = [record for record in records if record.role == "file"]
    previews = [record for record in records if record.role == "preview"]
    image_bytes = sum(record.size for record in images)
    file_bytes = sum(record.size for record in files)
    preview_bytes = sum(record.size for record in previews)
    total = len(records)
    total_bytes = sum(record.size for record in records)
    return {
        "images": len(images),
        "files": len(files),
        "preview": len(previews),
        "total": total,
        "image_count": len(images),
        "file_count": len(files),
        "preview_count": len(previews),
        "total_count": total,
        "image_bytes": image_bytes,
        "file_bytes": file_bytes,
        "preview_bytes": preview_bytes,
        "total_bytes": total_bytes,
    }


def build_manifest(
    product_root: Path,
    identity: ProductIdentity,
    revision: str,
    created_at: Optional[datetime_module.datetime] = None,
) -> Tuple[Dict[str, Any], bytes, str]:
    revision = _safe_component(revision, "revision")
    records = _collect_records(product_root)
    manifest: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "type": "etsy-cloud-asset-manifest",
        "product": identity.as_dict(),
        "revision": revision,
        "created_at": utc_text(created_at),
        "files": [record.as_dict() for record in records],
        "counts": _counts(records),
    }
    data = canonical_json_bytes(manifest)
    return manifest, data, sha256_bytes(data)


def _records_from_manifest(manifest: Mapping[str, Any]) -> List[AssetRecord]:
    if manifest.get("schema") != SCHEMA_VERSION or manifest.get("type") != "etsy-cloud-asset-manifest":
        raise AssetValidationError("unsupported cloud asset manifest schema")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise AssetValidationError("cloud asset manifest has no files")
    records = []
    seen = set()
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise AssetValidationError("cloud asset manifest file entry is invalid")
        path = _safe_relative(str(raw.get("path", "")))
        role = str(raw.get("role", ""))
        if role not in {"image", "file", "preview"}:
            raise AssetValidationError(f"invalid manifest file role: {role!r}")
        if path in seen:
            raise AssetValidationError(f"duplicate manifest file path: {path}")
        seen.add(path)
        try:
            size = int(raw["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AssetValidationError(f"invalid manifest size for {path}") from exc
        digest = str(raw.get("sha256", ""))
        if size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AssetValidationError(f"invalid manifest hash or size for {path}")
        records.append(AssetRecord(path, role, size, digest))
    return sorted(records, key=lambda item: (item.role, item.path))


def verify_manifest_directory(directory: Path, manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """Verify a staged revision's manifest and every content byte."""

    _ensure_directory(directory, "revision directory")
    expected = _records_from_manifest(manifest)
    actual = _collect_records(directory)
    if [record.as_dict() for record in actual] != [record.as_dict() for record in expected]:
        raise AssetValidationError("revision content does not match its canonical manifest")
    counts = manifest.get("counts")
    if not isinstance(counts, dict) or counts != _counts(expected):
        raise AssetValidationError("manifest aggregate counts do not match its file list")
    return {"files": len(actual), "bytes": sum(record.size for record in actual), "counts": counts}


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_local_state(product_root: Path, identity: Optional[ProductIdentity] = None) -> Dict[str, Any]:
    state_path = product_root / STATE_FILE_NAME
    if not state_path.exists():
        return {
            "schema": SCHEMA_VERSION,
            "product": identity.as_dict() if identity else None,
            "state": "LOCAL_ONLY",
            "current_revision": None,
            "current_manifest_sha256": None,
            "history": [],
        }
    try:
        with state_path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CloudAssetError(f"cannot read local cloud state: {state_path}") from exc
    if not isinstance(state, dict) or state.get("schema") != SCHEMA_VERSION:
        raise CloudAssetError(f"unsupported local cloud state: {state_path}")
    state.setdefault("history", [])
    state.setdefault("state", "LOCAL_ONLY")
    if state["state"] not in STATES:
        raise CloudAssetError(f"invalid local cloud state value: {state['state']!r}")
    return state


def save_local_state(product_root: Path, state: Mapping[str, Any]) -> None:
    payload = dict(state)
    payload["schema"] = SCHEMA_VERSION
    _write_bytes_atomic(product_root / STATE_FILE_NAME, canonical_json_bytes(payload))


def transition_state(
    state: Dict[str, Any],
    new_state: str,
    reason: str,
    now: Optional[datetime_module.datetime] = None,
    **details: Any,
) -> Dict[str, Any]:
    if new_state not in STATES:
        raise CloudAssetError(f"unknown cloud asset state: {new_state}")
    previous = str(state.get("state", "LOCAL_ONLY"))
    if new_state not in _STATE_TRANSITIONS.get(previous, {new_state}):
        raise CloudAssetError(f"invalid cloud asset transition: {previous} -> {new_state}")
    timestamp = utc_text(now)
    history = list(state.get("history") or [])
    entry: Dict[str, Any] = {
        "from": previous,
        "to": new_state,
        "at": timestamp,
        "reason": reason,
    }
    if details:
        entry["details"] = details
    history.append(entry)
    state["state"] = new_state
    state["updated_at"] = timestamp
    state["history"] = history[-50:]
    return state


class ProductLock:
    """A blocking, per-product POSIX lock kept beside local state."""

    def __init__(self, product_root: Path, timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> None:
        self.product_root = product_root
        self.timeout_seconds = timeout_seconds
        self._handle: Optional[Any] = None

    def __enter__(self) -> "ProductLock":
        lock_path = self.product_root / LOCK_FILE_NAME
        self._handle = lock_path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise CloudAssetError(f"timed out waiting for product lock: {lock_path}")
                time.sleep(0.05)

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


class CacheProductLock:
    """A per-product lock for directory hydration cache mutations."""

    def __init__(
        self,
        cache_root: Path,
        key: str,
        timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.cache_root = Path(cache_root).absolute()
        self.key = str(key)
        self.timeout_seconds = timeout_seconds
        self._handle: Optional[Any] = None

    @property
    def path(self) -> Path:
        digest = sha256_bytes(self.key.encode("utf-8"))
        return self.cache_root / HYDRATION_LOCK_DIR_NAME / f"{digest}.lock"

    def __enter__(self) -> "CacheProductLock":
        lock_dir = self.cache_root / HYDRATION_LOCK_DIR_NAME
        if lock_dir.exists() and lock_dir.is_symlink():
            raise AssetValidationError(f"symlinks are not allowed for hydration locks: {lock_dir}")
        lock_dir.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise CloudAssetError(f"timed out waiting for hydration cache lock: {self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


class RemoteStore:
    """Small remote interface implemented by rclone and deterministic tests."""

    def path_exists(self, remote_path: str) -> bool:
        raise NotImplementedError

    def upload_directory(self, local_directory: Path, remote_path: str) -> None:
        raise NotImplementedError

    def verify_directory(self, local_directory: Path, remote_path: str) -> None:
        raise NotImplementedError

    def download_directory(self, remote_path: str, local_directory: Path) -> None:
        raise NotImplementedError

    def read_bytes(self, remote_path: str) -> bytes:
        raise NotImplementedError

    def write_bytes(self, remote_path: str, data: bytes, overwrite: bool = True) -> None:
        raise NotImplementedError


class RcloneRemote(RemoteStore):
    """Google Drive remote accessed only through the existing rclone profile."""

    def __init__(
        self,
        repo_root: Path,
        remote: str = DEFAULT_REMOTE,
        parent_id: str = DEFAULT_PARENT_ID,
        rclone_bin: str = DEFAULT_RCLONE_BIN,
    ) -> None:
        if not REMOTE_NAME_PATTERN.fullmatch(remote):
            raise RemoteStoreError(f"unsafe rclone remote name: {remote!r}")
        if not DRIVE_ID_PATTERN.fullmatch(parent_id):
            raise RemoteStoreError("invalid Drive parent folder ID")
        self.repo_root = Path(repo_root).absolute()
        self.remote = remote
        self.parent_id = parent_id
        self.rclone_bin = rclone_bin

    def _remote_path(self, remote_path: str) -> str:
        return f"{self.remote}:{_safe_remote_relative(remote_path)}"

    def _run(self, args: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
        command = [self.rclone_bin, *args, "--drive-root-folder-id", self.parent_id]
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise RemoteStoreError(f"cannot execute rclone: {self.rclone_bin}") from exc
        if check and result.returncode:
            detail = _redact_error(result.stderr.strip() or result.stdout.strip())
            raise RemoteStoreError(f"rclone failed ({result.returncode}): {detail}")
        return result

    def path_exists(self, remote_path: str) -> bool:
        result = self._run(["lsjson", self._remote_path(remote_path), "--max-depth", "1"], check=False)
        return result.returncode == 0 and bool(result.stdout.strip()) and result.stdout.strip() != "[]"

    def upload_directory(self, local_directory: Path, remote_path: str) -> None:
        if self.path_exists(remote_path):
            raise RemoteConflictError(f"immutable remote revision already exists: {remote_path}")
        self._run(
            [
                "copy",
                str(local_directory),
                self._remote_path(remote_path),
                "--immutable",
                "--checkers",
                "4",
                "--transfers",
                "2",
            ]
        )

    def verify_directory(self, local_directory: Path, remote_path: str) -> None:
        self._run(
            [
                "check",
                str(local_directory),
                self._remote_path(remote_path),
                "--checksum",
                "--checkers",
                "4",
            ]
        )

    def download_directory(self, remote_path: str, local_directory: Path) -> None:
        local_directory.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "copy",
                self._remote_path(remote_path),
                str(local_directory),
                "--checksum",
                "--checkers",
                "4",
                "--transfers",
                "2",
            ]
        )

    def read_bytes(self, remote_path: str) -> bytes:
        result = self._run(["cat", self._remote_path(remote_path)])
        return result.stdout.encode("utf-8")

    def write_bytes(self, remote_path: str, data: bytes, overwrite: bool = True) -> None:
        with tempfile.TemporaryDirectory(prefix="etsy-cloud-pointer-") as temporary:
            source = Path(temporary) / "current.json"
            source.write_bytes(data)
            args = ["copyto", str(source), self._remote_path(remote_path)]
            if not overwrite:
                # A migration publishes current.json only after its immutable
                # revision is verified.  --immutable makes that publication
                # create-only, so another host cannot be silently overwritten
                # between our existence probe and this write.
                args.append("--immutable")
            self._run(args)


def _copy_tree_files(source: Path, destination: Path, records: Sequence[AssetRecord]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for record in records:
        source_path = source / Path(record.path)
        target_path = destination / Path(record.path)
        _validate_regular_file(source_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)


def _compare_tree(left: Path, right: Path) -> None:
    def files_under(root: Path) -> Dict[str, Path]:
        result: Dict[str, Path] = {}
        for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            for name in list(dirnames):
                child = current_path / name
                if child.is_symlink():
                    raise RemoteStoreError(f"symlinks are not allowed in local remote: {child}")
            for name in filenames:
                child = current_path / name
                if child.is_symlink():
                    raise RemoteStoreError(f"symlinks are not allowed in local remote: {child}")
                result[child.relative_to(root).as_posix()] = child
        return result

    left_files = files_under(left)
    right_files = files_under(right)
    if set(left_files) != set(right_files):
        raise RemoteStoreError("remote verification failed: file set differs")
    for name in sorted(left_files):
        left_path = left_files[name]
        right_path = right_files[name]
        if left_path.stat().st_size != right_path.stat().st_size or sha256_file(left_path) != sha256_file(right_path):
            raise RemoteStoreError(f"remote verification failed: content differs for {name}")


class LocalRemote(RemoteStore):
    """Filesystem remote used by tests and offline development only."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()
        self.operations: List[Tuple[str, str]] = []

    def _target(self, remote_path: str) -> Path:
        relative = Path(_safe_remote_relative(remote_path))
        target = self.root / relative
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise RemoteStoreError("local remote path escaped its root") from exc
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise RemoteStoreError(f"remote symlink is not allowed: {current}")
        return target

    def path_exists(self, remote_path: str) -> bool:
        return self._target(remote_path).exists()

    def upload_directory(self, local_directory: Path, remote_path: str) -> None:
        target = self._target(remote_path)
        if target.exists() or target.is_symlink():
            raise RemoteConflictError(f"immutable remote revision already exists: {remote_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(local_directory, target, symlinks=False)
        self.operations.append(("upload", remote_path))

    def verify_directory(self, local_directory: Path, remote_path: str) -> None:
        target = self._target(remote_path)
        if not target.is_dir():
            raise RemoteStoreError(f"remote directory is missing: {remote_path}")
        _compare_tree(local_directory, target)
        self.operations.append(("verify", remote_path))

    def download_directory(self, remote_path: str, local_directory: Path) -> None:
        source = self._target(remote_path)
        if not source.is_dir():
            raise RemoteStoreError(f"remote directory is missing: {remote_path}")
        local_directory.mkdir(parents=True, exist_ok=True)
        for current, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
            current_path = Path(current)
            relative = current_path.relative_to(source)
            destination = local_directory / relative
            destination.mkdir(parents=True, exist_ok=True)
            for name in dirnames:
                child = current_path / name
                if child.is_symlink():
                    raise RemoteStoreError(f"remote symlink is not allowed: {child}")
            for name in filenames:
                child = current_path / name
                if child.is_symlink():
                    raise RemoteStoreError(f"remote symlink is not allowed: {child}")
                shutil.copy2(child, destination / name)
        self.operations.append(("download", remote_path))

    def read_bytes(self, remote_path: str) -> bytes:
        target = self._target(remote_path)
        if target.is_symlink() or not target.is_file():
            raise RemoteStoreError(f"remote file is missing: {remote_path}")
        return target.read_bytes()

    def write_bytes(self, remote_path: str, data: bytes, overwrite: bool = True) -> None:
        target = self._target(remote_path)
        if target.exists() and not overwrite:
            raise RemoteConflictError(f"remote file already exists: {remote_path}")
        _write_bytes_atomic(target, data)
        self.operations.append(("write", remote_path))


@dataclass(frozen=True)
class CacheLookup:
    key: str
    status: str
    hit: bool
    expired: bool
    data_path: Optional[Path]
    metadata: Mapping[str, Any]


class HydrationCache:
    """Local cache primitives for future dashboard hydration.

    Each key gets a hashed payload and JSON metadata.  Success and failure
    entries have separate TTLs, so repeated failed hydration does not hammer
    Drive while a later retry can still recover naturally.
    """

    def __init__(
        self,
        root: Path,
        success_ttl_seconds: int = DEFAULT_SUCCESS_TTL_SECONDS,
        failure_ttl_seconds: int = DEFAULT_FAILURE_TTL_SECONDS,
    ) -> None:
        self.root = Path(root).absolute()
        self.success_ttl_seconds = success_ttl_seconds
        self.failure_ttl_seconds = failure_ttl_seconds
        self.data_root = self.root / "data"
        self.metadata_root = self.root / "metadata"

    def _paths(self, key: str) -> Tuple[Path, Path]:
        text = str(key)
        if not text or "\x00" in text:
            raise CloudAssetError("cache key must be non-empty")
        digest = sha256_bytes(text.encode("utf-8"))
        return self.data_root / f"{digest}.bin", self.metadata_root / f"{digest}.json"

    def lookup(self, key: str, now: Optional[datetime_module.datetime] = None) -> CacheLookup:
        data_path, metadata_path = self._paths(key)
        if not metadata_path.is_file():
            return CacheLookup(key, "miss", False, False, None, {})
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return CacheLookup(key, "miss", False, False, None, {})
        if not isinstance(metadata, dict):
            return CacheLookup(key, "miss", False, False, None, {})
        expires = parse_utc(metadata.get("expires_at"))
        expired = expires is None or expires <= (now or utc_now())
        status = str(metadata.get("status", "failure"))
        if status == "success" and not expired and data_path.is_file():
            return CacheLookup(key, status, True, False, data_path, metadata)
        if status == "failure" and not expired:
            return CacheLookup(key, status, True, False, None, metadata)
        return CacheLookup(key, status, False, expired, None, metadata)

    def store_success(
        self,
        key: str,
        source: Union[Path, bytes, bytearray],
        now: Optional[datetime_module.datetime] = None,
        ttl_seconds: Optional[int] = None,
        expected_sha256: Optional[str] = None,
    ) -> CacheLookup:
        data_path, metadata_path = self._paths(key)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.metadata_root.mkdir(parents=True, exist_ok=True)
        temporary = data_path.with_name(f".{data_path.name}.tmp-{uuid.uuid4().hex}")
        if isinstance(source, Path):
            _validate_regular_file(source)
            digest = sha256_file(source)
            with source.open("rb") as source_handle, temporary.open("wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
        else:
            data = bytes(source)
            if not data:
                raise AssetValidationError("zero-byte hydration result is not allowed")
            digest = sha256_bytes(data)
            temporary.write_bytes(data)
        if expected_sha256 and digest != expected_sha256:
            temporary.unlink(missing_ok=True)
            raise AssetValidationError("hydration result hash does not match expected SHA-256")
        os.replace(temporary, data_path)
        created = now or utc_now()
        expires = created + datetime_module.timedelta(seconds=ttl_seconds or self.success_ttl_seconds)
        metadata = {
            "schema": SCHEMA_VERSION,
            "key": str(key),
            "status": "success",
            "created_at": utc_text(created),
            "expires_at": utc_text(expires),
            "sha256": digest,
            "size": data_path.stat().st_size,
            "data_file": data_path.name,
        }
        _write_bytes_atomic(metadata_path, canonical_json_bytes(metadata))
        return CacheLookup(key, "success", True, False, data_path, metadata)

    def store_failure(
        self,
        key: str,
        error: str,
        now: Optional[datetime_module.datetime] = None,
        ttl_seconds: Optional[int] = None,
    ) -> CacheLookup:
        _, metadata_path = self._paths(key)
        self.metadata_root.mkdir(parents=True, exist_ok=True)
        created = now or utc_now()
        expires = created + datetime_module.timedelta(seconds=ttl_seconds or self.failure_ttl_seconds)
        metadata = {
            "schema": SCHEMA_VERSION,
            "key": str(key),
            "status": "failure",
            "created_at": utc_text(created),
            "expires_at": utc_text(expires),
            "error": _redact_error(str(error)),
        }
        _write_bytes_atomic(metadata_path, canonical_json_bytes(metadata))
        return CacheLookup(key, "failure", True, False, None, metadata)

    def hydrate(
        self,
        key: str,
        loader: Callable[[], Union[Path, bytes, bytearray]],
        now: Optional[datetime_module.datetime] = None,
        expected_sha256: Optional[str] = None,
    ) -> CacheLookup:
        cached = self.lookup(key, now=now)
        if cached.hit:
            return cached
        try:
            result = loader()
            return self.store_success(key, result, now=now, expected_sha256=expected_sha256)
        except Exception as exc:  # noqa: BLE001 - cache must retain failure metadata before re-raising
            self.store_failure(key, str(exc), now=now)
            raise


class CloudAssetStore:
    """Coordinate local validation, immutable revisions, and safe offload."""

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        remote: str = DEFAULT_REMOTE,
        parent_id: str = DEFAULT_PARENT_ID,
        rclone_bin: str = DEFAULT_RCLONE_BIN,
        remote_store: Optional[RemoteStore] = None,
        cache_root: Optional[Path] = None,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
        success_ttl_seconds: int = DEFAULT_SUCCESS_TTL_SECONDS,
        failure_ttl_seconds: int = DEFAULT_FAILURE_TTL_SECONDS,
        offload_age_days: int = DEFAULT_OFFLOAD_AGE_DAYS,
    ) -> None:
        self.repo_root = Path(repo_root or Path(__file__).resolve().parent).expanduser().absolute()
        self.remote_name = remote
        self.parent_id = parent_id
        self.rclone_bin = rclone_bin
        if offload_age_days < DEFAULT_OFFLOAD_AGE_DAYS:
            raise CloudAssetError(
                f"offload_age_days cannot be less than {DEFAULT_OFFLOAD_AGE_DAYS}"
            )
        self.offload_age_days = int(offload_age_days)
        self.remote = remote_store or RcloneRemote(self.repo_root, remote, parent_id, rclone_bin)
        resolved_cache = Path(cache_root or (self.repo_root / DEFAULT_CACHE_RELATIVE)).expanduser()
        if not resolved_cache.is_absolute():
            resolved_cache = self.repo_root / resolved_cache
        self.cache = HydrationCache(
            resolved_cache,
            success_ttl_seconds=success_ttl_seconds,
            failure_ttl_seconds=failure_ttl_seconds,
        )
        self.lock_timeout_seconds = lock_timeout_seconds

    def _resolve(self, product_root: Union[str, Path]) -> Tuple[Path, ProductIdentity]:
        return resolve_product(self.repo_root, product_root)

    @staticmethod
    def _revision_path(identity: ProductIdentity, revision: str) -> str:
        revision = _safe_component(revision, "revision")
        return f"{identity.remote_prefix}/revisions/{revision}"

    @classmethod
    def _manifest_path(cls, identity: ProductIdentity, revision: str) -> str:
        return f"{cls._revision_path(identity, revision)}/manifest.json"

    @classmethod
    def _current_path(cls, identity: ProductIdentity) -> str:
        return f"{identity.remote_prefix}/current.json"

    def _new_state(self, identity: ProductIdentity) -> Dict[str, Any]:
        return load_local_state(self.repo_root / identity.key, identity)

    def _write_receipt(
        self,
        operation: str,
        identity: ProductIdentity,
        state_before: str,
        state_after: str,
        details: Mapping[str, Any],
        now: Optional[datetime_module.datetime] = None,
    ) -> Path:
        timestamp = now or utc_now()
        stamp = timestamp.astimezone(datetime_module.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_key = identity.key.replace("/", "-")
        receipt = {
            "schema": SCHEMA_VERSION,
            "type": "etsy-cloud-asset-audit-receipt",
            "operation": operation,
            "at": utc_text(timestamp),
            "product": identity.as_dict(),
            "state_before": state_before,
            "state_after": state_after,
            "remote": self.remote_name,
            "details": dict(details),
        }
        destination = self.cache.root / "audit" / f"{stamp}-{operation}-{safe_key}-{uuid.uuid4().hex[:10]}.json"
        _write_bytes_atomic(destination, canonical_json_bytes(receipt))
        return destination

    def _transition(
        self,
        product_root: Path,
        state: Dict[str, Any],
        new_state: str,
        reason: str,
        now: Optional[datetime_module.datetime] = None,
        **details: Any,
    ) -> None:
        transition_state(state, new_state, reason, now=now, **details)
        save_local_state(product_root, state)

    def _decode_manifest(self, data: bytes, expected_sha256: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
        digest = sha256_bytes(data)
        if expected_sha256 and digest != expected_sha256:
            raise RemoteStoreError("remote manifest hash does not match current.json")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteStoreError("remote manifest is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise RemoteStoreError("remote manifest is not a JSON object")
        if canonical_json_bytes(value) != data:
            raise RemoteStoreError("remote manifest is not canonical JSON")
        _records_from_manifest(value)
        return value, digest

    def _read_current_and_manifest(self, identity: ProductIdentity) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
        current_path = self._current_path(identity)
        if not self.remote.path_exists(current_path):
            raise RemoteStoreError(f"remote current pointer is missing: {current_path}")
        try:
            pointer_value = json.loads(self.remote.read_bytes(current_path).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteStoreError("remote current pointer is not valid JSON") from exc
        if not isinstance(pointer_value, dict):
            raise RemoteStoreError("remote current pointer is not a JSON object")
        if pointer_value.get("schema") != SCHEMA_VERSION or pointer_value.get("product") != identity.key:
            raise RemoteStoreError("remote current pointer has the wrong product or schema")
        revision = _safe_component(str(pointer_value.get("revision", "")), "revision")
        revision_path = str(pointer_value.get("revision_path", ""))
        expected_path = self._revision_path(identity, revision)
        if revision_path != expected_path:
            raise RemoteStoreError("remote current pointer has an unsafe revision path")
        expected_hash = str(pointer_value.get("manifest_sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise RemoteStoreError("remote current pointer has an invalid manifest hash")
        manifest_data = self.remote.read_bytes(self._manifest_path(identity, revision))
        manifest, digest = self._decode_manifest(manifest_data, expected_sha256=expected_hash)
        if manifest.get("revision") != revision:
            raise RemoteStoreError("remote manifest revision does not match current.json")
        product = manifest.get("product")
        if not isinstance(product, dict) or product.get("key") != identity.key:
            raise RemoteStoreError("remote manifest product does not match current.json")
        return pointer_value, manifest, digest

    def resolve_current_manifest(self, product_root: Union[str, Path]) -> Dict[str, Any]:
        """Read and validate the immutable remote current pointer and manifest.

        This is the public read-only boundary for integrations that need the
        current cloud revision.  Browser/posting code should use
        :meth:`resolve_asset_root` instead of reaching into private methods.
        """

        product_path, identity = self._resolve(product_root)
        with ProductLock(product_path, self.lock_timeout_seconds):
            pointer, manifest, digest = self._read_current_and_manifest(identity)
        return {
            "ok": True,
            "product": identity.key,
            "scope": identity.scope,
            "shop": identity.shop,
            "product_name": identity.product,
            "revision": pointer["revision"],
            "revision_path": pointer["revision_path"],
            "manifest_sha256": digest,
            "pointer": pointer,
            "manifest": manifest,
        }

    @staticmethod
    def _hydration_cache_key(identity: ProductIdentity, revision: str) -> str:
        return f"{identity.key}@{_safe_component(revision, 'revision')}"

    def _hydration_cache_path(self, identity: ProductIdentity, revision: str) -> Path:
        revision = _safe_component(revision, "revision")
        bucket = identity.shop if identity.scope == "shops" else "master"
        # Keep hydrated bytes under the documented persistent data subtree;
        # nothing in this path points back into the product checkout.
        path = self.cache.data_root / identity.scope / _safe_component(bucket, "cache scope") / identity.product / revision
        root = self.cache.root.absolute()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise AssetValidationError("hydration cache path escaped its root") from exc
        return path

    def _hydration_metadata_path(self, key: str) -> Path:
        if not str(key).strip() or "\x00" in str(key):
            raise AssetValidationError("hydration cache key must be non-empty")
        metadata_root = self.cache.root / HYDRATION_METADATA_DIR_NAME
        if metadata_root.exists() and metadata_root.is_symlink():
            raise AssetValidationError(f"symlinks are not allowed for hydration metadata: {metadata_root}")
        return metadata_root / f"{sha256_bytes(str(key).encode('utf-8'))}.json"

    def _ensure_hydration_cache_parent(self, path: Path) -> None:
        root = self.cache.root.absolute()
        if root.exists() and root.is_symlink():
            raise AssetValidationError(f"symlinks are not allowed for hydration cache: {root}")
        root.mkdir(parents=True, exist_ok=True)
        relative = path.absolute().relative_to(root)
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            if current.exists() and current.is_symlink():
                raise AssetValidationError(f"symlinks are not allowed in hydration cache: {current}")
            current.mkdir(exist_ok=True)

    def _write_hydration_metadata(self, key: str, metadata: Mapping[str, Any]) -> Path:
        destination = self._hydration_metadata_path(key)
        self._ensure_hydration_cache_parent(destination)
        payload = dict(metadata)
        payload.update({"schema": SCHEMA_VERSION, "key": str(key)})
        _write_bytes_atomic(destination, canonical_json_bytes(payload))
        return destination

    def _load_hydration_metadata(self, key: str) -> Optional[Dict[str, Any]]:
        destination = self._hydration_metadata_path(key)
        if not destination.is_file() or destination.is_symlink():
            return None
        try:
            value = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("schema") != SCHEMA_VERSION or value.get("key") != str(key):
            return None
        return value

    def _cache_asset_paths(
        self,
        asset_root: Path,
        manifest: Mapping[str, Any],
    ) -> Dict[str, Any]:
        records = _records_from_manifest(manifest)
        images: List[str] = []
        files: List[str] = []
        preview: Optional[str] = None
        for record in records:
            path = asset_root / Path(record.path)
            _validate_regular_file(path)
            if record.role == "image":
                images.append(str(path))
            elif record.role == "file":
                files.append(str(path))
            elif record.role == "preview":
                preview = str(path)
        if not images or not files:
            raise AssetValidationError("hydrated manifest must contain images and files")
        return {
            "root": str(asset_root),
            "images_dir": str(asset_root / "images"),
            "files_dir": str(asset_root / "files"),
            "preview_path": preview,
            "image_paths": sorted(images),
            "file_paths": sorted(files),
        }

    def _local_asset_paths(self, product_root: Path) -> Dict[str, Any]:
        """Collect safe local paths without requiring a Phase 1 manifest."""

        images: List[str] = []
        files: List[str] = []
        for dirname, target in (("images", images), ("files", files)):
            directory = product_root / dirname
            if directory.is_symlink():
                raise AssetValidationError(f"symlinks are not allowed: {directory}")
            if not directory.exists():
                continue
            _ensure_directory(directory, dirname)
            for current, dirnames, filenames in os.walk(directory, topdown=True, followlinks=False):
                current_path = Path(current)
                safe_dirnames: List[str] = []
                for name in sorted(dirnames):
                    child = current_path / name
                    if child.is_symlink():
                        raise AssetValidationError(f"symlinks are not allowed: {child}")
                    safe_dirnames.append(name)
                dirnames[:] = safe_dirnames
                for name in sorted(filenames):
                    if name == ".DS_Store":
                        continue
                    path = current_path / name
                    _validate_regular_file(path)
                    target.append(str(path))

        preview_path = product_root / PREVIEW_FILE_NAME
        preview = None
        if preview_path.exists() or preview_path.is_symlink():
            _validate_regular_file(preview_path)
            preview = str(preview_path)
        return {
            "root": str(product_root),
            "images_dir": str(product_root / "images"),
            "files_dir": str(product_root / "files"),
            "preview_path": preview,
            "image_paths": sorted(images),
            "file_paths": sorted(files),
        }

    def _resolution_payload(
        self,
        product_path: Path,
        identity: ProductIdentity,
        state: Mapping[str, Any],
        paths: Mapping[str, Any],
        *,
        source: str,
        revision: Optional[str],
        manifest: Optional[Mapping[str, Any]],
        manifest_sha256: Optional[str],
        cache_key: Optional[str] = None,
        cache_metadata: Optional[Mapping[str, Any]] = None,
        pointer: Optional[Mapping[str, Any]] = None,
        purpose: Optional[str] = None,
    ) -> Dict[str, Any]:
        image_paths = list(paths.get("image_paths", []))
        file_paths = list(paths.get("file_paths", []))
        counts = dict(manifest.get("counts") or {}) if isinstance(manifest, Mapping) else {}
        if not counts:
            records: List[AssetRecord] = []
            for role, values in (("image", image_paths), ("file", file_paths)):
                for raw_path in values:
                    path = Path(raw_path)
                    info = _validate_regular_file(path)
                    records.append(
                        AssetRecord(
                            path.relative_to(product_path).as_posix(),
                            role,
                            int(info.st_size),
                            sha256_file(path),
                        )
                    )
            if paths.get("preview_path"):
                preview_path = Path(str(paths["preview_path"]))
                info = _validate_regular_file(preview_path)
                records.append(
                    AssetRecord(
                        PREVIEW_FILE_NAME,
                        "preview",
                        int(info.st_size),
                        sha256_file(preview_path),
                    )
                )
            counts = _counts(records)
        cleanup_metadata = {
            "required": source == "cloud-cache",
            "successful_operation_required": source == "cloud-cache",
            "cache_key": cache_key,
            "cache_path": str(paths["root"]) if source == "cloud-cache" else None,
            "expires_at": (cache_metadata or {}).get("expires_at"),
            "eligible_at": (cache_metadata or {}).get("cleanup_eligible_at"),
            "eligible": bool((cache_metadata or {}).get("cleanup_eligible_at")),
            "success_ttl_seconds": self.cache.success_ttl_seconds if source == "cloud-cache" else None,
            "failure_ttl_seconds": self.cache.failure_ttl_seconds if source == "cloud-cache" else None,
        }
        mode = "local" if source == "local" else "cloud"
        return {
            "ok": True,
            "product": identity.key,
            "identity": identity.as_dict(),
            "scope": identity.scope,
            "shop": identity.shop,
            "product_name": identity.product,
            "state": state.get("state", "LOCAL_ONLY"),
            "purpose": purpose,
            "mode": mode,
            "source": source,
            "hydrated": mode == "cloud",
            "cache_hit": bool(cache_metadata and cache_metadata.get("cache_hit")),
            "product_root": str(product_path),
            "asset_root": str(paths["root"]),
            "images_dir": str(paths["images_dir"]),
            "files_dir": str(paths["files_dir"]),
            "preview_path": paths.get("preview_path"),
            "preview_available": bool(paths.get("preview_path")),
            # The explicit lists are the browser/post-script contract.  Keep
            # image_paths/file_paths as backwards-compatible aliases.
            "images": image_paths,
            "files": file_paths,
            "image_paths": image_paths,
            "file_paths": file_paths,
            "paths": {
                "root": str(paths["root"]),
                "images": str(paths["images_dir"]),
                "files": str(paths["files_dir"]),
                "preview": paths.get("preview_path"),
                "image_paths": image_paths,
                "file_paths": file_paths,
                "images_list": image_paths,
                "files_list": file_paths,
            },
            "revision": revision,
            "manifest_sha256": manifest_sha256,
            "manifest_hash": manifest_sha256,
            "hash": manifest_sha256,
            "manifest": dict(manifest) if isinstance(manifest, Mapping) else None,
            "pointer": dict(pointer) if isinstance(pointer, Mapping) else None,
            "current_pointer": dict(pointer) if isinstance(pointer, Mapping) else None,
            "current": dict(pointer) if isinstance(pointer, Mapping) else None,
            "counts": counts,
            "bytes": int(counts.get("total_bytes", 0) or 0),
            "local_available": bool(image_paths or file_paths),
            "cloud_available": bool(
                pointer
                or state.get("current_revision")
                or state.get("cloud_revision")
                or source == "cloud-cache"
            ),
            "cache_key": cache_key,
            "cache_path": str(paths["root"]) if source == "cloud-cache" else None,
            "cache_expires_at": (cache_metadata or {}).get("expires_at"),
            "cache_cleanup_eligible_at": (cache_metadata or {}).get("cleanup_eligible_at"),
            "cleanup": cleanup_metadata,
            "cleanup_metadata": cleanup_metadata,
            "last_error": state.get("last_error"),
        }

    def _cached_hydration_lookup(
        self,
        identity: ProductIdentity,
        revision: str,
        manifest_sha256: str,
        now: datetime_module.datetime,
    ) -> Dict[str, Any]:
        key = self._hydration_cache_key(identity, revision)
        metadata = self._load_hydration_metadata(key)
        if not metadata:
            return {"key": key, "status": "miss", "hit": False, "metadata": {}}
        expires = parse_utc(metadata.get("expires_at"))
        expired = expires is None or expires <= now
        status = str(metadata.get("status") or "failure")
        if status == "failure" and not expired:
            return {"key": key, "status": status, "hit": True, "expired": False, "metadata": metadata}
        if status != "success" or expired:
            return {"key": key, "status": status, "hit": False, "expired": expired, "metadata": metadata}

        cache_path = self._hydration_cache_path(identity, revision)
        if cache_path.is_symlink() or not cache_path.is_dir():
            return {
                "key": key,
                "status": "corrupt",
                "hit": False,
                "expired": False,
                "metadata": metadata,
                "error": "hydration cache directory is missing or unsafe",
            }
        try:
            manifest_path = cache_path / "manifest.json"
            _validate_regular_file(manifest_path)
            cached_manifest, cached_digest = self._decode_manifest(
                manifest_path.read_bytes(), expected_sha256=manifest_sha256
            )
            if cached_digest != manifest_sha256:
                raise RemoteStoreError("hydration cache manifest hash does not match current revision")
            verification = verify_manifest_directory(cache_path, cached_manifest)
            paths = self._cache_asset_paths(cache_path, cached_manifest)
        except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError) as exc:
            return {
                "key": key,
                "status": "corrupt",
                "hit": False,
                "expired": False,
                "metadata": metadata,
                "error": _redact_error(str(exc)),
            }
        return {
            "key": key,
            "status": "success",
            "hit": True,
            "expired": False,
            "metadata": {**metadata, "cache_hit": True, "verification": verification},
            "paths": paths,
            "manifest": cached_manifest,
            "manifest_sha256": cached_digest,
            "cache_path": cache_path,
        }

    def _write_hydration_failure(
        self,
        identity: ProductIdentity,
        revision: str,
        error: str,
        now: datetime_module.datetime,
        purpose: Optional[str] = None,
    ) -> Dict[str, Any]:
        key = self._hydration_cache_key(identity, revision)
        existing = self._load_hydration_metadata(key)
        existing_expires = parse_utc(existing.get("expires_at")) if existing else None
        if existing and existing.get("status") == "failure" and existing_expires and existing_expires > now:
            return existing
        expires = now + datetime_module.timedelta(seconds=self.cache.failure_ttl_seconds)
        metadata = {
            "type": "etsy-cloud-hydration-cache",
            "status": "failure",
            "created_at": utc_text(now),
            "expires_at": utc_text(expires),
            "error": _redact_error(error),
            "identity": identity.as_dict(),
            "revision": revision if revision != "current" else None,
            "purpose": purpose,
        }
        self._write_hydration_metadata(key, metadata)
        return metadata

    def _write_hydration_success(
        self,
        identity: ProductIdentity,
        revision: str,
        manifest: Mapping[str, Any],
        manifest_sha256: str,
        cache_path: Path,
        now: datetime_module.datetime,
        purpose: Optional[str] = None,
    ) -> Dict[str, Any]:
        key = self._hydration_cache_key(identity, revision)
        expires = now + datetime_module.timedelta(seconds=self.cache.success_ttl_seconds)
        metadata = {
            "type": "etsy-cloud-hydration-cache",
            "status": "success",
            "created_at": utc_text(now),
            "expires_at": utc_text(expires),
            "identity": identity.as_dict(),
            "revision": revision,
            "manifest_sha256": manifest_sha256,
            "cache_path": str(cache_path),
            "counts": dict(manifest.get("counts") or {}),
            "cleanup_eligible_at": None,
            "purpose": purpose,
        }
        self._write_hydration_metadata(key, metadata)
        return metadata

    def _remove_hydration_cache_path(self, identity: ProductIdentity, revision: str) -> None:
        path = self._hydration_cache_path(identity, revision)
        if path.is_symlink():
            raise AssetValidationError(f"symlinks are not allowed in hydration cache: {path}")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def _download_to_hydration_cache(
        self,
        identity: ProductIdentity,
        pointer: Mapping[str, Any],
        manifest: Mapping[str, Any],
        manifest_sha256: str,
        now: datetime_module.datetime,
        purpose: Optional[str] = None,
    ) -> Tuple[Path, Dict[str, Any]]:
        revision = _safe_component(str(pointer["revision"]), "revision")
        cache_path = self._hydration_cache_path(identity, revision)
        self._ensure_hydration_cache_parent(cache_path)
        cache_parent = cache_path.parent
        cache_parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{revision}.hydrate-", dir=str(cache_parent)))
        try:
            self.remote.download_directory(self._revision_path(identity, revision), temporary)
            manifest_path = temporary / "manifest.json"
            _validate_regular_file(manifest_path)
            downloaded_manifest, downloaded_digest = self._decode_manifest(
                manifest_path.read_bytes(), expected_sha256=manifest_sha256
            )
            if downloaded_manifest != dict(manifest) or downloaded_digest != manifest_sha256:
                raise RemoteStoreError("downloaded remote manifest differs from current.json")
            verification = verify_manifest_directory(temporary, downloaded_manifest)
            self.remote.verify_directory(temporary, self._revision_path(identity, revision))
            pointer_after, manifest_after, digest_after = self._read_current_and_manifest(identity)
            if (
                pointer_after.get("revision") != revision
                or manifest_after != dict(manifest)
                or digest_after != manifest_sha256
            ):
                raise RemoteStoreError("remote current revision changed during hydration")
            if cache_path.exists() or cache_path.is_symlink():
                self._remove_hydration_cache_path(identity, revision)
            os.replace(temporary, cache_path)
            metadata = self._write_hydration_success(
                identity,
                revision,
                downloaded_manifest,
                downloaded_digest,
                cache_path,
                now,
                purpose=purpose,
            )
            metadata["verification"] = verification
            return cache_path, metadata
        finally:
            if temporary.exists() or temporary.is_symlink():
                shutil.rmtree(temporary, ignore_errors=True)

    def _local_hydration_candidate(
        self,
        product_path: Path,
        identity: ProductIdentity,
        state: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        candidate_manifest = state.get("candidate_manifest")
        candidate_revision = state.get("candidate_revision")
        if isinstance(candidate_manifest, dict) and candidate_revision:
            try:
                if self._local_matches(product_path, candidate_manifest):
                    paths = self._cache_asset_paths(product_path, candidate_manifest)
                    return self._resolution_payload(
                        product_path,
                        identity,
                        state,
                        paths,
                        source="local",
                        revision=str(candidate_revision),
                        manifest=candidate_manifest,
                        manifest_sha256=str(state.get("candidate_manifest_sha256") or sha256_bytes(canonical_json_bytes(candidate_manifest))),
                    )
            except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError):
                pass

        current_manifest = state.get("current_manifest")
        current_revision = state.get("current_revision")
        if isinstance(current_manifest, dict) and current_revision and self._local_has_both_content_dirs(product_path):
            try:
                if self._local_matches(product_path, current_manifest):
                    paths = self._cache_asset_paths(product_path, current_manifest)
                    return self._resolution_payload(
                        product_path,
                        identity,
                        state,
                        paths,
                        source="local",
                        revision=str(current_revision),
                        manifest=current_manifest,
                        manifest_sha256=str(state.get("current_manifest_sha256") or sha256_bytes(canonical_json_bytes(current_manifest))),
                    )
            except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError):
                pass

        if not current_revision and str(state.get("state") or "LOCAL_ONLY") not in {"CLOUD_ONLY", "ERROR", "RESTORING"}:
            paths = self._local_asset_paths(product_path)
            return self._resolution_payload(
                product_path,
                identity,
                state,
                paths,
                source="local",
                revision=str(candidate_revision) if candidate_revision else None,
                manifest=candidate_manifest if isinstance(candidate_manifest, dict) else None,
                manifest_sha256=str(state.get("candidate_manifest_sha256") or "") or None,
            )
        return None

    @staticmethod
    def _normalize_hydration_purpose(purpose: Optional[str]) -> str:
        value = DEFAULT_HYDRATION_PURPOSE if purpose is None else str(purpose).strip()
        if not value or "\x00" in value or len(value) > 128:
            raise AssetValidationError("hydration purpose must be a non-empty short label")
        return value

    def _active_hydration_failure(
        self,
        key: str,
        now: datetime_module.datetime,
    ) -> Optional[Dict[str, Any]]:
        metadata = self._load_hydration_metadata(key)
        if not metadata or metadata.get("status") != "failure":
            return None
        expires = parse_utc(metadata.get("expires_at"))
        if expires is None or expires <= now:
            return None
        return metadata

    def _clear_hydration_failure(self, key: str) -> None:
        """Remove one stale failure marker after its remote pointer recovers."""

        destination = self._hydration_metadata_path(key)
        if destination.is_symlink():
            raise AssetValidationError(f"symlinks are not allowed for hydration metadata: {destination}")
        try:
            if destination.is_file():
                destination.unlink()
        except FileNotFoundError:
            # Another cleanup of this exact marker won the race.
            return
        except OSError:
            # A recovered pointer is still usable even if stale audit metadata
            # cannot be removed; the next call will retry this narrow cleanup.
            return

    def _hydrate_product_impl(
        self,
        product_root: Union[str, Path],
        *,
        purpose: Optional[str],
        now: Optional[datetime_module.datetime],
        allow_local_only: bool,
    ) -> Dict[str, Any]:
        """Implement strict hydration while retaining the legacy local-only path."""

        normalized_purpose = self._normalize_hydration_purpose(purpose)
        product_path, identity = self._resolve(product_root)
        timestamp = now or utc_now()
        failure_revision: Optional[str] = None
        with ProductLock(product_path, self.lock_timeout_seconds):
            state = load_local_state(product_path, identity)
            try:
                # Existing posting callers may use local-only products before
                # a cloud revision exists.  The new hydrate_product API never
                # takes this branch; it always resolves current.json first.
                if allow_local_only and isinstance(state.get("candidate_manifest"), dict) and state.get("candidate_revision"):
                    try:
                        if self._local_matches(product_path, state["candidate_manifest"]):
                            local_result = self._local_hydration_candidate(product_path, identity, state)
                            if local_result is not None:
                                local_result["purpose"] = normalized_purpose
                                return local_result
                    except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError):
                        pass
                if (
                    allow_local_only
                    and not state.get("current_revision")
                    and not state.get("cloud_revision")
                    and str(state.get("state") or "LOCAL_ONLY")
                    not in {"CLOUD_ONLY", "ERROR", "RESTORING"}
                ):
                    local_result = self._local_hydration_candidate(product_path, identity, state)
                    if local_result is not None:
                        local_result["purpose"] = normalized_purpose
                        return local_result

                # A retained current-pointer failure is fail-closed for the
                # seven-day failure window.  This avoids repeatedly probing a
                # known-bad remote pointer while leaving the metadata for audit.
                failure_revision = "current"
                pointer_failure = self._active_hydration_failure(
                    self._hydration_cache_key(identity, "current"),
                    timestamp,
                )
                try:
                    pointer, manifest, manifest_sha256 = self._read_current_and_manifest(identity)
                except Exception:
                    failure_revision = "current"
                    if pointer_failure:
                        error = str(pointer_failure.get("error") or "previous current pointer failure")
                        raise RemoteStoreError(f"cloud asset hydration retry is retained: {error}")
                    raise
                if pointer_failure:
                    self._clear_hydration_failure(self._hydration_cache_key(identity, "current"))
                revision = _safe_component(str(pointer["revision"]), "revision")

                # A complete local product is usable only when every local
                # byte matches the canonical current manifest.  Dirty or
                # unsafe local content is never silently replaced by cache.
                local_present = self._local_has_both_content_dirs(product_path)
                local_any = self._local_has_any_content(product_path)
                if local_present:
                    try:
                        local_matches = self._local_matches(product_path, manifest)
                    except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError):
                        local_matches = False
                    if local_matches:
                        if state.get("state") == "ERROR":
                            self._transition(
                                product_path,
                                state,
                                "CLOUD_VERIFIED",
                                "current cloud manifest recovered and local assets match",
                                timestamp,
                                revision=revision,
                            )
                        state.update(
                            {
                                "current_revision": revision,
                                "current_manifest_sha256": manifest_sha256,
                                "current_manifest": manifest,
                                "last_error": None,
                            }
                        )
                        save_local_state(product_path, state)
                        paths = self._cache_asset_paths(product_path, manifest)
                        return self._resolution_payload(
                            product_path,
                            identity,
                            state,
                            paths,
                            source="local",
                            revision=revision,
                            manifest=manifest,
                            manifest_sha256=manifest_sha256,
                            pointer=pointer,
                            purpose=normalized_purpose,
                        )
                if local_any:
                    # Invoke the validator to preserve a precise failure for
                    # zero-byte/dataless/symlink local entries where possible.
                    self._local_asset_paths(product_path)
                    raise AssetValidationError("local assets do not match the current cloud manifest")

                key = self._hydration_cache_key(identity, revision)
                failure_revision = revision
                with CacheProductLock(self.cache.root, key, self.lock_timeout_seconds):
                    cached = self._cached_hydration_lookup(
                        identity,
                        revision,
                        manifest_sha256,
                        timestamp,
                    )
                    if cached.get("hit") and cached.get("status") == "failure":
                        error = str(cached.get("metadata", {}).get("error") or "previous hydration failed")
                        raise RemoteStoreError(f"cloud asset hydration retry is retained: {error}")
                    if cached.get("hit") and cached.get("status") == "success":
                        paths = cached["paths"]
                        result = self._resolution_payload(
                            product_path,
                            identity,
                            state,
                            paths,
                            source="cloud-cache",
                            revision=revision,
                            manifest=cached["manifest"],
                            manifest_sha256=cached["manifest_sha256"],
                            cache_key=key,
                            cache_metadata=cached["metadata"],
                            pointer=pointer,
                            purpose=normalized_purpose,
                        )
                        state["last_hydrated_at"] = utc_text(timestamp)
                        state["last_hydrated_revision"] = revision
                        state["last_error"] = None
                        save_local_state(product_path, state)
                        return result

                    if cached.get("status") == "corrupt":
                        self._remove_hydration_cache_path(identity, revision)
                    cache_path, metadata = self._download_to_hydration_cache(
                        identity,
                        pointer,
                        manifest,
                        manifest_sha256,
                        timestamp,
                        purpose=normalized_purpose,
                    )
                    hydrated_manifest_path = cache_path / "manifest.json"
                    hydrated_manifest, hydrated_digest = self._decode_manifest(
                        hydrated_manifest_path.read_bytes(), expected_sha256=manifest_sha256
                    )
                    paths = self._cache_asset_paths(cache_path, hydrated_manifest)
                    state["last_hydrated_at"] = utc_text(timestamp)
                    state["last_hydrated_revision"] = revision
                    state["last_error"] = None
                    save_local_state(product_path, state)
                    return self._resolution_payload(
                        product_path,
                        identity,
                        state,
                        paths,
                        source="cloud-cache",
                        revision=revision,
                        manifest=hydrated_manifest,
                        manifest_sha256=hydrated_digest,
                        cache_key=key,
                        cache_metadata=metadata,
                        pointer=pointer,
                        purpose=normalized_purpose,
                    )
            except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError) as exc:
                error_text = _redact_error(str(exc))
                if failure_revision is not None:
                    try:
                        self._write_hydration_failure(
                            identity,
                            failure_revision,
                            error_text,
                            timestamp,
                            purpose=normalized_purpose,
                        )
                    except Exception:
                        pass
                state["last_error"] = error_text
                try:
                    transition_state(state, "ERROR", "asset hydration failed", now=timestamp, error=error_text)
                except CloudAssetError:
                    state["state"] = "ERROR"
                    state["updated_at"] = utc_text(timestamp)
                save_local_state(product_path, state)
                raise

    def resolve_asset_root(
        self,
        product_root: Union[str, Path],
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        """Resolve a safe local or verified cloud-cache product root.

        This is the backwards-compatible resolver used by existing posting
        code.  It retains support for complete local-only products; products
        with a cloud revision use the same current-pointer and cache checks as
        :meth:`hydrate_product`.
        """

        return self._hydrate_product_impl(
            product_root,
            purpose="legacy-resolver",
            now=now,
            allow_local_only=True,
        )

    def hydrate_product(
        self,
        product_root: Union[str, Path],
        purpose: str = DEFAULT_HYDRATION_PURPOSE,
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        """Return verified product paths for a browser/post operation.

        The canonical product identity, ``current.json`` pointer, immutable
        revision manifest, and every asset SHA-256 are checked before paths
        are returned.  Matching local assets are returned with ``mode=local``.
        If local content is absent or ``CLOUD_ONLY``, the immutable revision is
        downloaded into ``output/cloud-cache/data/.../<revision>/`` (or the
        configured cache root) and returned with ``mode=cloud``; the product
        folder is never modified.  The result always includes explicit
        ``images`` and ``files`` lists suitable for Playwright's
        ``set_input_files`` plus backwards-compatible path aliases.

        ``purpose`` is an audit-friendly consumer label and does not affect
        cache identity, so browser and posting callers can share a verified
        revision cache safely.
        """

        # Before Phase 2A, the second positional argument was ``now``.  Keep
        # that call shape working for callers that used the compatibility
        # alias before purpose was introduced.
        if isinstance(purpose, datetime_module.datetime) and now is None:
            now = purpose
            purpose = DEFAULT_HYDRATION_PURPOSE
        return self._hydrate_product_impl(
            product_root,
            purpose=purpose,
            now=now,
            allow_local_only=False,
        )

    def resolve_product_assets(
        self,
        product_root: Union[str, Path],
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        """Public alias used by non-browser asset collectors."""

        return self.resolve_asset_root(product_root, now=now)

    def hydration_cache_status(
        self,
        product_root: Union[str, Path],
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        """Return cache-only metadata without contacting the remote."""

        product_path, identity = self._resolve(product_root)
        timestamp = now or utc_now()
        state = load_local_state(product_path, identity)
        revision = str(state.get("current_revision") or "")
        if not revision:
            return {"available": False, "status": "none", "product": identity.key}
        manifest_sha256 = str(state.get("current_manifest_sha256") or "")
        lookup = self._cached_hydration_lookup(identity, revision, manifest_sha256, timestamp)
        return {
            "available": bool(lookup.get("hit") and lookup.get("status") == "success"),
            "status": lookup.get("status", "miss"),
            "product": identity.key,
            "revision": revision,
            "cache_path": str(lookup.get("cache_path")) if lookup.get("cache_path") else str(self._hydration_cache_path(identity, revision)),
            "expires_at": lookup.get("metadata", {}).get("expires_at"),
            "cleanup_eligible_at": lookup.get("metadata", {}).get("cleanup_eligible_at"),
            "last_error": lookup.get("metadata", {}).get("error") if lookup.get("status") == "failure" else None,
        }

    def mark_hydration_cleanup_eligible(
        self,
        resolution: Optional[Mapping[str, Any]],
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        """Mark a successful cache result eligible for later TTL cleanup.

        Hydrated bytes are intentionally retained until the consumer reports
        a successful browser/post operation.  This helper only writes the
        eligibility marker; it never removes product-local source assets.
        """

        if not isinstance(resolution, Mapping) or resolution.get("source") != "cloud-cache":
            return {"ok": True, "marked": False, "reason": "no cloud cache result"}
        key = str(resolution.get("cache_key") or "").strip()
        if not key:
            return {"ok": True, "marked": False, "reason": "missing cache key"}
        timestamp = now or utc_now()
        with CacheProductLock(self.cache.root, key, self.lock_timeout_seconds):
            metadata = self._load_hydration_metadata(key)
            if not metadata or metadata.get("status") != "success":
                return {"ok": False, "marked": False, "reason": "cache success metadata is missing"}
            expires = parse_utc(metadata.get("expires_at"))
            if expires is None or expires <= timestamp:
                return {"ok": False, "marked": False, "reason": "cache success TTL has expired"}
            identity_raw = metadata.get("identity") or {}
            identity = ProductIdentity(
                str(identity_raw.get("scope") or ""),
                str(identity_raw.get("product") or ""),
                str(identity_raw.get("shop")) if identity_raw.get("shop") else None,
            )
            revision = _safe_component(str(metadata.get("revision") or ""), "revision")
            cache_path = self._hydration_cache_path(identity, revision)
            if cache_path.is_symlink() or not cache_path.is_dir():
                return {"ok": False, "marked": False, "reason": "successful cache entry is missing or unsafe"}
            metadata["cleanup_eligible_at"] = utc_text(timestamp)
            self._write_hydration_metadata(key, metadata)
            return {
                "ok": True,
                "marked": True,
                "cache_key": key,
                "cleanup_eligible_at": metadata["cleanup_eligible_at"],
            }

    def cleanup_hydration_cache(
        self,
        now: Optional[datetime_module.datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Remove expired cache entries only after a successful operation.

        Success entries require ``mark_hydration_cleanup_eligible`` before
        their configured success TTL can remove the revision directory.
        Failure metadata is retained until its configured failure TTL, then
        only the metadata is removed.  This helper never touches product
        ``images/`` or ``files/`` directories.
        """

        timestamp = now or utc_now()
        metadata_root = self.cache.root / HYDRATION_METADATA_DIR_NAME
        if metadata_root.is_symlink() or not metadata_root.is_dir():
            return []
        results: List[Dict[str, Any]] = []
        for metadata_path in sorted(metadata_root.glob("*.json")):
            if metadata_path.is_symlink():
                results.append({"ok": False, "path": str(metadata_path), "reason": "metadata symlink"})
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                expires = parse_utc(metadata.get("expires_at")) if isinstance(metadata, dict) else None
                if not isinstance(metadata, dict) or expires is None or expires > timestamp:
                    continue
                status = str(metadata.get("status") or "")
                # Unknown metadata is never deleted by a safety helper.
                if status not in {"success", "failure"}:
                    continue
                eligible_at = parse_utc(metadata.get("cleanup_eligible_at"))
                if status == "success" and (eligible_at is None or eligible_at > timestamp):
                    # Expiration alone is not proof that the browser/post
                    # consumer completed successfully.
                    continue
                key = str(metadata.get("key") or "").strip()
                if not key:
                    raise AssetValidationError("hydration metadata has no cache key")
                with CacheProductLock(self.cache.root, key, self.lock_timeout_seconds):
                    current = self._load_hydration_metadata(key)
                    if not current or current.get("status") != status:
                        continue
                    current_expires = parse_utc(current.get("expires_at"))
                    if current_expires is None or current_expires > timestamp:
                        continue
                    if status == "success":
                        current_eligible_at = parse_utc(current.get("cleanup_eligible_at"))
                        if current_eligible_at is None or current_eligible_at > timestamp:
                            continue
                        identity_raw = current.get("identity") or {}
                        identity = ProductIdentity(
                            str(identity_raw.get("scope") or ""),
                            str(identity_raw.get("product") or ""),
                            str(identity_raw.get("shop")) if identity_raw.get("shop") else None,
                        )
                        revision = _safe_component(str(current.get("revision") or ""), "revision")
                        self._remove_hydration_cache_path(identity, revision)
                    metadata_path.unlink()
                    results.append(
                        {
                            "ok": True,
                            "key": current.get("key"),
                            "status": status,
                            "expired": True,
                            "removed_cache": status == "success",
                        }
                    )
            except (AssetValidationError, OSError, ValueError, TypeError, KeyError) as exc:
                results.append({"ok": False, "path": str(metadata_path), "reason": _redact_error(str(exc))})
        return results

    def cleanup_cache(self, now: Optional[datetime_module.datetime] = None) -> List[Dict[str, Any]]:
        """Short public alias for hydration cache TTL cleanup."""

        return self.cleanup_hydration_cache(now=now)

    def cancel_offload(
        self,
        product_root: Union[str, Path],
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        """Cancel a scheduled offload without touching remote or Etsy state."""

        product_path, identity = self._resolve(product_root)
        timestamp = now or utc_now()
        with ProductLock(product_path, self.lock_timeout_seconds):
            state = load_local_state(product_path, identity)
            if state.get("state") != "OFFLOAD_SCHEDULED":
                return {
                    "ok": True,
                    "product": identity.key,
                    "state": state.get("state", "LOCAL_ONLY"),
                    "cancelled": False,
                }
            local_matches = False
            if isinstance(state.get("current_manifest"), dict):
                try:
                    local_matches = self._local_matches(product_path, state["current_manifest"])
                except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError):
                    local_matches = False
            target_state = "CLOUD_VERIFIED" if local_matches else "DIRTY_LOCAL"
            self._transition(product_path, state, target_state, "offload cancelled", timestamp)
            state["eligible_after"] = None
            state["last_error"] = None
            save_local_state(product_path, state)
            return {
                "ok": True,
                "product": identity.key,
                "state": state["state"],
                "cancelled": True,
            }

    def record_local_candidate(
        self,
        product_root: Union[str, Path],
        now: Optional[datetime_module.datetime] = None,
        reason: str = "local asset sync completed; cloud upload candidate created",
    ) -> Dict[str, Any]:
        """Record a complete local revision as DIRTY_LOCAL without uploading it."""

        product_path, identity = self._resolve(product_root)
        timestamp = now or utc_now()
        with ProductLock(product_path, self.lock_timeout_seconds):
            state = load_local_state(product_path, identity)
            state_before = str(state.get("state", "LOCAL_ONLY"))
            try:
                _, pending_data, pending_digest = build_manifest(
                    product_path,
                    identity,
                    "pending",
                    created_at=timestamp,
                )
                candidate_revision = f"local-{timestamp.astimezone(datetime_module.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{pending_digest[:16]}"
                manifest, manifest_data, manifest_sha256 = build_manifest(
                    product_path,
                    identity,
                    candidate_revision,
                    created_at=timestamp,
                )
                if manifest_data != canonical_json_bytes(manifest):
                    raise AssetValidationError("local candidate manifest serialization changed")
                previous_revision = state.get("current_revision")
                if previous_revision:
                    state["cloud_revision"] = previous_revision
                    state["cloud_manifest"] = state.get("current_manifest")
                    state["cloud_manifest_sha256"] = state.get("current_manifest_sha256")
                state.update(
                    {
                        "product": identity.as_dict(),
                        "candidate_revision": candidate_revision,
                        "candidate_manifest": manifest,
                        "candidate_manifest_sha256": manifest_sha256,
                        "pending_upload_revision": candidate_revision,
                        "pending_upload": True,
                        "last_local_revision": candidate_revision,
                        "last_local_revision_at": utc_text(timestamp),
                        "last_error": None,
                    }
                )
                transition_state(state, "DIRTY_LOCAL", reason, now=timestamp, revision=candidate_revision)
                save_local_state(product_path, state)
                receipt = self._write_receipt(
                    "record-local-candidate",
                    identity,
                    state_before,
                    str(state["state"]),
                    {"revision": candidate_revision, "manifest_sha256": manifest_sha256},
                    timestamp,
                )
                state["last_receipt"] = str(receipt)
                save_local_state(product_path, state)
                return {
                    "ok": True,
                    "product": identity.key,
                    "state": state["state"],
                    "revision": candidate_revision,
                    "candidate_revision": candidate_revision,
                    "manifest_sha256": manifest_sha256,
                    "receipt": str(receipt),
                }
            except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError) as exc:
                state["last_error"] = _redact_error(str(exc))
                try:
                    transition_state(state, "ERROR", "local candidate recording failed", now=timestamp, error=str(exc))
                except CloudAssetError:
                    state["state"] = "ERROR"
                save_local_state(product_path, state)
                raise

    @contextlib.contextmanager
    def _local_revision_stage(
        self,
        product_root: Path,
        manifest: Mapping[str, Any],
        prefix: str = "etsy-cloud-upload-",
    ) -> Iterator[Path]:
        records = _records_from_manifest(manifest)
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            stage = Path(temporary)
            _copy_tree_files(product_root, stage, records)
            _write_bytes_atomic(stage / "manifest.json", canonical_json_bytes(manifest))
            verify_manifest_directory(stage, manifest)
            yield stage

    @contextlib.contextmanager
    def _remote_restore_stage(
        self,
        identity: ProductIdentity,
        revision: str,
        prefix: str = "etsy-cloud-restore-",
        parent: Optional[Path] = None,
    ) -> Iterator[Path]:
        directory_parent = parent or self.cache.root
        directory_parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=prefix, dir=str(directory_parent)))
        try:
            self.remote.download_directory(self._revision_path(identity, revision), temporary)
            yield temporary
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _verify_remote_revision(
        self,
        identity: ProductIdentity,
        pointer: Mapping[str, Any],
        manifest: Mapping[str, Any],
        local_stage: Optional[Path] = None,
    ) -> Dict[str, Any]:
        revision = _safe_component(str(pointer["revision"]), "revision")
        if local_stage is not None:
            verify_manifest_directory(local_stage, manifest)
            self.remote.verify_directory(local_stage, self._revision_path(identity, revision))
            remote_manifest_data = self.remote.read_bytes(self._manifest_path(identity, revision))
            if remote_manifest_data != canonical_json_bytes(manifest):
                raise RemoteStoreError("remote manifest changed during verification")
            return {"mode": "local", "revision": revision, "manifest_sha256": pointer["manifest_sha256"]}
        with self._remote_restore_stage(identity, revision) as restored:
            manifest_path = restored / "manifest.json"
            _validate_regular_file(manifest_path)
            downloaded_manifest, digest = self._decode_manifest(manifest_path.read_bytes(), pointer["manifest_sha256"])
            verify_manifest_directory(restored, downloaded_manifest)
            self.remote.verify_directory(restored, self._revision_path(identity, revision))
            return {"mode": "download", "revision": revision, "manifest_sha256": digest}

    @staticmethod
    def _local_matches(product_root: Path, manifest: Mapping[str, Any]) -> bool:
        expected = _records_from_manifest(manifest)
        actual = _collect_records(product_root)
        return [item.as_dict() for item in actual] == [item.as_dict() for item in expected]

    @staticmethod
    def _local_has_both_content_dirs(product_root: Path) -> bool:
        return all((product_root / name).is_dir() and not (product_root / name).is_symlink() for name in CONTENT_DIRS)

    @staticmethod
    def _local_has_any_content(product_root: Path) -> bool:
        for name in CONTENT_DIRS:
            directory = product_root / name
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                return True
            if directory.is_dir() and any(directory.iterdir()):
                return True
        return False

    @staticmethod
    def _has_current_verified_revision(state: Mapping[str, Any], identity: ProductIdentity) -> bool:
        """Check the local proof needed for an empty-directory offload retry.

        This deliberately validates only the locally persisted revision proof;
        it does not contact the remote.  The worker's existing idempotent
        ``offload_now`` path remains responsible for remote re-verification.
        """

        if state.get("state") not in {"CLOUD_ONLY", "CLEANUP_PENDING"}:
            return False
        revision = str(state.get("current_revision") or "").strip()
        manifest_hash = str(state.get("current_manifest_sha256") or "").strip()
        manifest = state.get("current_manifest")
        if not revision or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash) or not isinstance(manifest, dict):
            return False
        if manifest.get("revision") != revision:
            return False
        product = manifest.get("product")
        if not isinstance(product, dict) or product.get("key") != identity.key:
            return False
        try:
            records = _records_from_manifest(manifest)
        except (AssetValidationError, TypeError, KeyError, ValueError):
            return False
        if not {record.role for record in records}.issuperset({"image", "file"}):
            return False
        counts = manifest.get("counts")
        return (
            isinstance(counts, dict)
            and counts == _counts(records)
            and sha256_bytes(canonical_json_bytes(manifest)) == manifest_hash
        )

    def preflight_upload_and_offload(
        self,
        product_root: Union[str, Path],
    ) -> Dict[str, Any]:
        """Validate a new destructive admission without contacting the remote.

        New work must have both usable asset groups.  An already verified
        cloud-only/cleanup-pending product with no local content is the one
        intentional exception: it is an idempotent cleanup retry and is
        checked again by ``upload_and_offload`` before any local deletion.
        """

        product_path, identity = self._resolve(product_root)
        with ProductLock(product_path, self.lock_timeout_seconds):
            state = load_local_state(product_path, identity)
            local_empty_after_offload = self._local_content_dirs_empty(product_path)
            if (
                self._has_current_verified_revision(state, identity)
                and not self._local_has_any_content(product_path)
                and (
                    not self._local_has_both_content_dirs(product_path)
                    or local_empty_after_offload
                )
            ):
                return {
                    "ok": True,
                    "product": identity.key,
                    "state": state.get("state"),
                    "revision": state.get("current_revision"),
                    "retry": True,
                    "counts": dict((state.get("current_manifest") or {}).get("counts") or {}),
                }

            records = _collect_records(product_path)
            return {
                "ok": True,
                "product": identity.key,
                "state": state.get("state", "LOCAL_ONLY"),
                "retry": False,
                "counts": _counts(records),
            }

    @staticmethod
    def _revision_for_upload(now: datetime_module.datetime, manifest_sha256: str) -> str:
        stamp = now.astimezone(datetime_module.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{manifest_sha256[:16]}"

    def upload(
        self,
        product_root: Union[str, Path],
        revision: Optional[str] = None,
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        product_path, identity = self._resolve(product_root)
        timestamp = now or utc_now()
        with ProductLock(product_path, self.lock_timeout_seconds):
            state = load_local_state(product_path, identity)
            state_before = str(state.get("state", "LOCAL_ONLY"))
            # Keep a small local preview beside the marker/state so the
            # dashboard can still render a thumbnail after a later offload.
            # Conversion is best-effort and never relaxes the full asset
            # validation or remote verification gates below.
            preview_created = _ensure_cloud_preview(product_path)
            try:
                # A content-identical upload without an explicit revision is
                # idempotent.  Reuse the recorded canonical manifest so its
                # created_at field does not manufacture a new revision.
                if revision is None and state.get("current_revision") and state.get("current_manifest"):
                    try:
                        existing_manifest = state["current_manifest"]
                        pointer, current_manifest, current_digest = self._read_current_and_manifest(identity)
                        if (
                            pointer.get("revision") == state.get("current_revision")
                            and current_manifest == existing_manifest
                            and self._local_matches(product_path, existing_manifest)
                        ):
                            with self._local_revision_stage(product_path, existing_manifest) as stage:
                                verification = self._verify_remote_revision(identity, pointer, existing_manifest, stage)
                            self._transition(
                                product_path,
                                state,
                                "CLOUD_VERIFIED",
                                "idempotent upload reverified existing revision",
                                timestamp,
                                revision=pointer["revision"],
                            )
                            state.update(
                                {
                                    "cloud_verified_at": utc_text(timestamp),
                                    "last_error": None,
                                }
                            )
                            save_local_state(product_path, state)
                            receipt = self._write_receipt(
                                "upload",
                                identity,
                                state_before,
                                str(state["state"]),
                                {
                                    "result": "already_current",
                                    "revision": pointer["revision"],
                                    "manifest_sha256": current_digest,
                                    "verification": verification,
                                    "preview_created": preview_created,
                                },
                                timestamp,
                            )
                            return {
                                "ok": True,
                                "product": identity.key,
                                "revision": pointer["revision"],
                                "manifest_sha256": current_digest,
                                "state": state["state"],
                                "receipt": str(receipt),
                                "idempotent": True,
                                "preview_created": preview_created,
                            }
                    except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError):
                        pass
                chosen_revision = _safe_component(revision, "revision") if revision else "pending"
                manifest, manifest_data, manifest_sha256 = build_manifest(
                    product_path, identity, chosen_revision, created_at=timestamp
                )
                if revision is None:
                    chosen_revision = self._revision_for_upload(timestamp, manifest_sha256)
                    manifest, manifest_data, manifest_sha256 = build_manifest(
                        product_path, identity, chosen_revision, created_at=timestamp
                    )
                else:
                    manifest["revision"] = chosen_revision
                    manifest_data = canonical_json_bytes(manifest)
                    manifest_sha256 = sha256_bytes(manifest_data)

                if state.get("current_manifest_sha256") == manifest_sha256 and state.get("current_revision"):
                    current_revision = str(state["current_revision"])
                    pointer, current_manifest, _ = self._read_current_and_manifest(identity)
                    if pointer.get("revision") == current_revision and current_manifest == manifest:
                        with self._local_revision_stage(product_path, manifest) as stage:
                            verification = self._verify_remote_revision(identity, pointer, manifest, stage)
                        self._transition(
                            product_path,
                            state,
                            "CLOUD_VERIFIED",
                            "idempotent upload reverified existing revision",
                            timestamp,
                            revision=current_revision,
                        )
                        state.update(
                            {
                                "cloud_verified_at": utc_text(timestamp),
                                "last_error": None,
                            }
                        )
                        save_local_state(product_path, state)
                        receipt = self._write_receipt(
                            "upload",
                            identity,
                            state_before,
                            str(state["state"]),
                            {
                                "result": "already_current",
                                "revision": current_revision,
                                "verification": verification,
                                "preview_created": preview_created,
                            },
                            timestamp,
                        )
                        return {
                            "ok": True,
                            "product": identity.key,
                            "revision": current_revision,
                            "manifest_sha256": manifest_sha256,
                            "state": state["state"],
                            "receipt": str(receipt),
                            "idempotent": True,
                            "preview_created": preview_created,
                        }

                if state_before in {"CLOUD_VERIFIED", "READY_LOCAL"} and state.get("current_manifest_sha256"):
                    try:
                        if not self._local_matches(product_path, state.get("current_manifest") or {}):
                            self._transition(product_path, state, "DIRTY_LOCAL", "local assets changed before upload", timestamp)
                    except (AssetValidationError, TypeError, KeyError):
                        self._transition(product_path, state, "DIRTY_LOCAL", "local assets are incomplete before upload", timestamp)
                self._transition(
                    product_path,
                    state,
                    "UPLOADING",
                    "immutable revision upload started",
                    timestamp,
                    revision=chosen_revision,
                    manifest_sha256=manifest_sha256,
                )
                revision_path = self._revision_path(identity, chosen_revision)
                with self._local_revision_stage(product_path, manifest) as stage:
                    if self.remote.path_exists(revision_path):
                        raise RemoteConflictError(f"immutable remote revision already exists: {revision_path}")
                    self.remote.upload_directory(stage, revision_path)
                    self.remote.verify_directory(stage, revision_path)
                    remote_manifest = self.remote.read_bytes(self._manifest_path(identity, chosen_revision))
                    if remote_manifest != manifest_data:
                        raise RemoteStoreError("remote manifest differs after upload")
                    pointer = {
                        "schema": SCHEMA_VERSION,
                        "type": "etsy-cloud-current-pointer",
                        "product": identity.key,
                        "revision": chosen_revision,
                        "revision_path": revision_path,
                        "manifest_sha256": manifest_sha256,
                        "verified_at": utc_text(timestamp),
                    }
                    pointer_data = canonical_json_bytes(pointer)
                    self.remote.write_bytes(self._current_path(identity), pointer_data, overwrite=True)
                    if self.remote.read_bytes(self._current_path(identity)) != pointer_data:
                        raise RemoteStoreError("remote current pointer read-back failed")

                self._transition(
                    product_path,
                    state,
                    "CLOUD_VERIFIED",
                    "immutable revision uploaded and remotely verified",
                    timestamp,
                    revision=chosen_revision,
                )
                state.update(
                    {
                        "product": identity.as_dict(),
                        "current_revision": chosen_revision,
                        "current_manifest_sha256": manifest_sha256,
                        "current_manifest": manifest,
                        "uploaded_at": utc_text(timestamp),
                        "cloud_verified_at": utc_text(timestamp),
                        # A new immutable revision has no restore-verification
                        # soak yet.  Only verify()/restore() may establish it.
                        "last_restore_verified_at": None,
                        "last_restore_verified_revision": None,
                        "eligible_after": None,
                        "last_error": None,
                    }
                )
                save_local_state(product_path, state)
                receipt = self._write_receipt(
                    "upload",
                    identity,
                    state_before,
                    str(state["state"]),
                    {
                        "result": "uploaded",
                        "revision": chosen_revision,
                        "manifest_sha256": manifest_sha256,
                        "counts": manifest["counts"],
                        "pointer_written_after_verification": True,
                        "preview_created": preview_created,
                    },
                    timestamp,
                )
                state["last_receipt"] = str(receipt)
                save_local_state(product_path, state)
                return {
                    "ok": True,
                    "product": identity.key,
                    "revision": chosen_revision,
                    "manifest_sha256": manifest_sha256,
                    "counts": manifest["counts"],
                    "state": state["state"],
                    "receipt": str(receipt),
                    "idempotent": False,
                    "preview_created": preview_created,
                }
            except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError) as exc:
                state["last_error"] = _redact_error(str(exc))
                try:
                    self._transition(product_path, state, "ERROR", "upload failed", timestamp, error=str(exc))
                except CloudAssetError:
                    save_local_state(product_path, state)
                receipt = self._write_receipt(
                    "upload",
                    identity,
                    state_before,
                    str(state.get("state", "ERROR")),
                    {"result": "error", "error": _redact_error(str(exc))},
                    timestamp,
                )
                state["last_receipt"] = str(receipt)
                save_local_state(product_path, state)
                raise

    @staticmethod
    def _offload_quarantine_path(product_path: Path) -> Path:
        return product_path.parent / f".{product_path.name}.cloud-offload-{uuid.uuid4().hex}"

    def _quarantine_local_assets(self, product_path: Path, quarantine: Optional[Path] = None) -> Path:
        """Move both content directories aside before committing CLOUD_ONLY."""

        quarantine = quarantine or self._offload_quarantine_path(product_path)
        quarantine.mkdir()
        moved: list[str] = []
        try:
            for dirname in CONTENT_DIRS:
                source = product_path / dirname
                if source.is_symlink() or not source.is_dir():
                    raise AssetValidationError(f"local {dirname} directory is missing or unsafe")
                os.replace(source, quarantine / dirname)
                moved.append(dirname)
                (product_path / dirname).mkdir()
        except (AssetValidationError, OSError):
            for dirname in reversed(moved):
                destination = product_path / dirname
                source = quarantine / dirname
                if destination.exists() or destination.is_symlink():
                    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
                        raise CloudAssetError(
                            f"cannot rollback over changed local {dirname} directory"
                        )
                    destination.rmdir()
                if source.exists() or source.is_symlink():
                    os.replace(source, destination)
            shutil.rmtree(quarantine, ignore_errors=True)
            raise
        return quarantine

    def _verify_quarantined_asset_groups(
        self,
        quarantine: Path,
        manifest: Mapping[str, Any],
    ) -> None:
        expected = [
            record
            for record in _records_from_manifest(manifest)
            if record.role in {"image", "file"}
        ]
        if set(CONTENT_DIRS) != {
            record.path.split("/", 1)[0]
            for record in expected
        }:
            raise AssetValidationError("quarantine manifest is missing images or files")
        actual = _collect_records(quarantine)
        if [record.as_dict() for record in actual] != [record.as_dict() for record in expected]:
            raise AssetValidationError("quarantine content no longer matches the pinned manifest")

    def _restore_quarantined_assets(
        self,
        product_path: Path,
        quarantine: Path,
        manifest: Mapping[str, Any],
    ) -> None:
        """Restore a pre-commit offload quarantine, failing closed on ambiguity."""

        if not all((quarantine / dirname).is_dir() and not (quarantine / dirname).is_symlink() for dirname in CONTENT_DIRS):
            raise CloudAssetError("offload quarantine is missing an asset group; recovery is required")
        self._verify_quarantined_asset_groups(quarantine, manifest)
        for dirname in reversed(CONTENT_DIRS):
            source = quarantine / dirname
            destination = product_path / dirname
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
                    raise CloudAssetError(f"cannot rollback offload over changed local {dirname} directory")
                destination.rmdir()
            os.replace(source, destination)
        if quarantine.exists() or quarantine.is_symlink():
            shutil.rmtree(quarantine)

    def _validate_cleanup_quarantine(self, product_path: Path, raw_path: Any) -> Path:
        if not raw_path:
            raise AssetValidationError("cleanup-pending state has no quarantine path")
        candidate = Path(str(raw_path)).expanduser().absolute()
        expected_parent = product_path.parent.absolute()
        expected_prefix = f".{product_path.name}.cloud-offload-"
        if candidate.parent != expected_parent or not candidate.name.startswith(expected_prefix):
            raise AssetValidationError("cleanup quarantine path is outside the product parent")
        return candidate

    @staticmethod
    def _local_content_dirs_empty(product_root: Path) -> bool:
        if not all(
            (product_root / name).is_dir() and not (product_root / name).is_symlink()
            for name in CONTENT_DIRS
        ):
            return False
        return all(not any((product_root / name).iterdir()) for name in CONTENT_DIRS)

    def _restore_state_snapshot(self, state_path: Path, snapshot: Optional[bytes]) -> None:
        if snapshot is None:
            state_path.unlink(missing_ok=True)
        else:
            _write_bytes_atomic(state_path, snapshot)

    def offload_now(
        self,
        product_root: Union[str, Path],
        expected_revision: Optional[str] = None,
        expected_manifest_sha256: Optional[str] = None,
        expected_product_key: Optional[str] = None,
        immediate_offload_authorized: bool = False,
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        """Verify an immutable revision, then immediately remove local assets.

        This is an explicit, user-confirmed workflow and is intentionally
        separate from :meth:`maintain`, whose seven-day policy and allowlist
        gates remain unchanged.  Local ``images`` and ``files`` are first
        quarantined on the same filesystem.  The state commit must succeed
        before the quarantine is removed, so a failed verification or state
        write does not turn into silent local data loss.
        """

        product_path, identity = self._resolve(product_root)
        if not immediate_offload_authorized:
            raise CloudAssetError("immediate offload requires explicit authorization")
        if expected_product_key != identity.key:
            raise CloudAssetError("immediate offload product confirmation does not match the resolved product")
        timestamp = now or utc_now()
        with ProductLock(product_path, self.lock_timeout_seconds):
            state = load_local_state(product_path, identity)
            state_before = str(state.get("state", "LOCAL_ONLY"))
            state_path = product_path / STATE_FILE_NAME
            state_snapshot = state_path.read_bytes() if state_path.is_file() else None
            quarantine: Optional[Path] = None
            try:
                pointer, manifest, manifest_sha256 = self._read_current_and_manifest(identity)
                if expected_revision is not None and str(pointer.get("revision")) != str(expected_revision):
                    raise RemoteConflictError("current cloud revision changed before local cleanup")
                if (
                    expected_manifest_sha256 is not None
                    and manifest_sha256 != str(expected_manifest_sha256)
                ):
                    raise RemoteConflictError("current cloud manifest changed before local cleanup")

                remote_verification = self._verify_remote_revision(identity, pointer, manifest)
                local_present = self._local_has_both_content_dirs(product_path)
                local_any = self._local_has_any_content(product_path)
                local_empty_after_offload = self._local_content_dirs_empty(product_path)

                if not local_present or (
                    local_empty_after_offload
                    and state_before in {"CLOUD_ONLY", "CLEANUP_PENDING"}
                ):
                    if local_any:
                        raise AssetValidationError("local images/files are incomplete; cleanup is blocked")
                    # A retry after a rare quarantine cleanup failure can finish
                    # the already-verified cleanup without uploading anything.
                    if state_before == "CLEANUP_PENDING" and state.get("cleanup_pending_path"):
                        pending = self._validate_cleanup_quarantine(
                            product_path,
                            state.get("cleanup_pending_path"),
                        )
                        try:
                            if pending.exists() or pending.is_symlink():
                                shutil.rmtree(pending)
                        except OSError as cleanup_error:
                            cleanup_message = _redact_error(str(cleanup_error))
                            state["last_error"] = cleanup_message
                            state["cleanup_pending_path"] = str(pending)
                            cleanup_receipt = self._write_receipt(
                                "offload-now",
                                identity,
                                state_before,
                                "CLEANUP_PENDING",
                                {
                                    "result": "cleanup_retry_pending",
                                    "revision": pointer["revision"],
                                    "manifest_sha256": manifest_sha256,
                                    "remote_verification": remote_verification,
                                    "quarantine": str(pending),
                                    "error": cleanup_message,
                                },
                                timestamp,
                            )
                            state["last_receipt"] = str(cleanup_receipt)
                            save_local_state(product_path, state)
                            return {
                                "ok": False,
                                "product": identity.key,
                                "revision": pointer["revision"],
                                "manifest_sha256": manifest_sha256,
                                "state": "CLEANUP_PENDING",
                                "remote_verified": True,
                                "offloaded": True,
                                "cleanup_pending": True,
                                "error": cleanup_message,
                                "receipt": str(cleanup_receipt),
                            }
                    if state.get("state") != "CLOUD_ONLY":
                        transition_state(
                            state,
                            "CLOUD_ONLY",
                            "remote revision verified and local content is already absent",
                            now=timestamp,
                            revision=pointer["revision"],
                        )
                    state.update(
                        {
                            "product": identity.as_dict(),
                            "current_revision": pointer["revision"],
                            "current_manifest_sha256": manifest_sha256,
                            "current_manifest": manifest,
                            "offloaded_at": utc_text(timestamp),
                            "last_local_matches": False,
                            "cleanup_pending_path": None,
                            "last_error": None,
                        }
                    )
                    receipt = self._write_receipt(
                        "offload-now",
                        identity,
                        state_before,
                        str(state["state"]),
                        {
                            "result": "already_offloaded",
                            "revision": pointer["revision"],
                            "manifest_sha256": manifest_sha256,
                            "remote_verification": remote_verification,
                            "deleted": ["images/*", "files/*"],
                        },
                        timestamp,
                    )
                    state["last_receipt"] = str(receipt)
                    save_local_state(product_path, state)
                    return {
                        "ok": True,
                        "product": identity.key,
                        "revision": pointer["revision"],
                        "manifest_sha256": manifest_sha256,
                        "state": state["state"],
                        "remote_verified": True,
                        "already_offloaded": True,
                        "deleted": ["images/*", "files/*"],
                        "receipt": str(receipt),
                    }

                if not self._local_matches(product_path, manifest):
                    raise AssetValidationError("local content does not match the verified cloud manifest")

                transition_state(
                    state,
                    "OFFLOAD_SCHEDULED",
                    "explicit upload-and-offload passed remote verification gates",
                    now=timestamp,
                    revision=pointer["revision"],
                )
                transition_state(
                    state,
                    "RESTORE_VERIFIED",
                    "temporary remote restore verified before immediate cleanup",
                    now=timestamp,
                    revision=pointer["revision"],
                )
                # This is the final local hash gate while ProductLock is held.
                if not self._local_matches(product_path, manifest):
                    raise AssetValidationError("local content changed before offload cleanup")

                quarantine = self._offload_quarantine_path(product_path)
                self._quarantine_local_assets(product_path, quarantine)
                transition_state(
                    state,
                    "CLOUD_ONLY",
                    "verified images and files moved to same-filesystem quarantine",
                    now=timestamp,
                    revision=pointer["revision"],
                )
                state.update(
                    {
                        "product": identity.as_dict(),
                        "current_revision": pointer["revision"],
                        "current_manifest_sha256": manifest_sha256,
                        "current_manifest": manifest,
                        "offloaded_at": utc_text(timestamp),
                        "last_local_matches": False,
                        "cleanup_pending_path": None,
                        "last_error": None,
                    }
                )
                receipt = self._write_receipt(
                    "offload-now",
                    identity,
                    state_before,
                    str(state["state"]),
                    {
                        "result": "offloaded",
                        "revision": pointer["revision"],
                        "manifest_sha256": manifest_sha256,
                        "remote_verification": remote_verification,
                        "deleted": ["images/*", "files/*"],
                        "quarantine": str(quarantine),
                    },
                    timestamp,
                )
                state["last_receipt"] = str(receipt)
                # The canonical state commit happens before deleting the
                # quarantine.  If this write fails, the outer handler restores
                # both the local directories and the original state bytes.
                save_local_state(product_path, state)
                try:
                    shutil.rmtree(quarantine)
                except OSError as cleanup_error:
                    cleanup_message = _redact_error(str(cleanup_error))
                    transition_state(
                        state,
                        "CLEANUP_PENDING",
                        "cloud-only commit succeeded but quarantine cleanup needs retry",
                        now=timestamp,
                        quarantine=str(quarantine),
                    )
                    state.update(
                        {
                            "cleanup_pending_path": str(quarantine),
                            "last_error": cleanup_message,
                        }
                    )
                    cleanup_receipt = self._write_receipt(
                        "offload-now",
                        identity,
                        "CLOUD_ONLY",
                        str(state["state"]),
                        {
                            "result": "cleanup_pending",
                            "revision": pointer["revision"],
                            "manifest_sha256": manifest_sha256,
                            "quarantine": str(quarantine),
                            "error": cleanup_message,
                        },
                        timestamp,
                    )
                    state["last_receipt"] = str(cleanup_receipt)
                    save_local_state(product_path, state)
                    quarantine = None
                    return {
                        "ok": False,
                        "product": identity.key,
                        "revision": pointer["revision"],
                        "manifest_sha256": manifest_sha256,
                        "state": state["state"],
                        "remote_verified": True,
                        "offloaded": True,
                        "cleanup_pending": True,
                        "error": cleanup_message,
                        "receipt": str(cleanup_receipt),
                    }
                quarantine = None
                return {
                    "ok": True,
                    "product": identity.key,
                    "revision": pointer["revision"],
                    "manifest_sha256": manifest_sha256,
                    "state": state["state"],
                    "remote_verified": True,
                    "offloaded": True,
                    "deleted": ["images/*", "files/*"],
                    "receipt": str(receipt),
                }
            except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError) as exc:
                rollback_error: Optional[Exception] = None
                if quarantine is not None and (quarantine.exists() or quarantine.is_symlink()):
                    try:
                        self._restore_quarantined_assets(product_path, quarantine, manifest)
                        quarantine = None
                    except Exception as restore_exc:  # noqa: BLE001 - preserve a failed-closed audit state
                        rollback_error = restore_exc
                elif quarantine is not None:
                    quarantine = None
                if rollback_error is None:
                    try:
                        self._restore_state_snapshot(state_path, state_snapshot)
                    except OSError as snapshot_error:
                        rollback_error = snapshot_error

                error_text = _redact_error(str(exc))
                if rollback_error is not None:
                    error_text = f"{error_text}; rollback failed: {_redact_error(str(rollback_error))}"
                try:
                    error_state = load_local_state(product_path, identity)
                except (CloudAssetError, OSError, ValueError, TypeError, KeyError):
                    error_state = state
                error_state["last_error"] = error_text
                if rollback_error is not None and quarantine is not None:
                    error_state["cleanup_pending_path"] = str(quarantine)
                    target_state = "CLEANUP_PENDING"
                else:
                    target_state = "ERROR"
                try:
                    transition_state(
                        error_state,
                        target_state,
                        "immediate offload failed; local content was preserved",
                        now=timestamp,
                        error=error_text,
                    )
                except CloudAssetError:
                    error_state["state"] = target_state
                save_local_state(product_path, error_state)
                try:
                    receipt = self._write_receipt(
                        "offload-now",
                        identity,
                        state_before,
                        str(error_state.get("state", target_state)),
                        {
                            "result": "error",
                            "error": error_text,
                            "local_preserved": rollback_error is None,
                            "cleanup_pending": rollback_error is not None,
                        },
                        timestamp,
                    )
                    error_state["last_receipt"] = str(receipt)
                    save_local_state(product_path, error_state)
                except (OSError, CloudAssetError, ValueError, TypeError, KeyError):
                    pass
                raise

    def upload_and_offload(
        self,
        product_root: Union[str, Path],
        revision: Optional[str] = None,
        expected_product_key: Optional[str] = None,
        immediate_offload_authorized: bool = False,
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        """Upload, remotely verify, and immediately offload local content."""

        product_path, identity = self._resolve(product_root)
        if not immediate_offload_authorized:
            raise CloudAssetError("upload and immediate offload requires explicit authorization")
        if expected_product_key != identity.key:
            raise CloudAssetError("immediate offload product confirmation does not match the resolved product")
        timestamp = now or utc_now()
        # Make an explicit retry idempotent after a successful cloud-only
        # commit or a quarantine cleanup interruption; never manufacture a new
        # revision from an absent local checkout.
        state = load_local_state(product_path, identity)
        local_empty_after_offload = self._local_content_dirs_empty(product_path)
        if state.get("state") in {"CLOUD_ONLY", "CLEANUP_PENDING"} and state.get("current_revision"):
            if (
                not self._local_has_any_content(product_path)
                and (not self._local_has_both_content_dirs(product_path) or local_empty_after_offload)
            ):
                return self.offload_now(
                    product_path,
                    expected_revision=str(state["current_revision"]),
                    expected_manifest_sha256=state.get("current_manifest_sha256"),
                    expected_product_key=identity.key,
                    immediate_offload_authorized=True,
                    now=timestamp,
                )

        upload_result = self.upload(product_path, revision=revision, now=timestamp)
        offload_result = self.offload_now(
            product_path,
            expected_revision=str(upload_result["revision"]),
            expected_manifest_sha256=str(upload_result["manifest_sha256"]),
            expected_product_key=identity.key,
            immediate_offload_authorized=True,
            now=timestamp,
        )
        return {
            "ok": bool(offload_result.get("ok", False)),
            "product": identity.key,
            "revision": offload_result.get("revision") or upload_result.get("revision"),
            "manifest_sha256": offload_result.get("manifest_sha256") or upload_result.get("manifest_sha256"),
            "state": offload_result.get("state"),
            "upload_result": upload_result,
            "offload_result": offload_result,
            "remote_verified": bool(offload_result.get("remote_verified")),
            "offloaded": bool(offload_result.get("offloaded") or offload_result.get("already_offloaded")),
            "cleanup_pending": bool(offload_result.get("cleanup_pending")),
            "receipt": offload_result.get("receipt"),
        }

    def verify(
        self,
        product_root: Union[str, Path],
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        product_path, identity = self._resolve(product_root)
        timestamp = now or utc_now()
        with ProductLock(product_path, self.lock_timeout_seconds):
            state = load_local_state(product_path, identity)
            state_before = str(state.get("state", "LOCAL_ONLY"))
            try:
                pointer, manifest, manifest_sha256 = self._read_current_and_manifest(identity)
                # A normal verify is deliberately a no-install restore
                # verification.  Downloading the immutable revision to a
                # temporary directory and validating every manifest hash is
                # the only event that starts the offload soak clock.
                remote_verification = self._verify_remote_revision(identity, pointer, manifest)
                local_present = self._local_has_both_content_dirs(product_path)
                local_any = self._local_has_any_content(product_path)
                local_matches = False
                local_error = None
                if local_present:
                    try:
                        local_matches = self._local_matches(product_path, manifest)
                    except (AssetValidationError, TypeError, KeyError) as exc:
                        local_error = str(exc)
                new_state = (
                    "OFFLOAD_SCHEDULED"
                    if local_matches
                    else ("DIRTY_LOCAL" if local_any else "CLOUD_ONLY")
                )
                transition_reason = (
                    "temporary restore verified and local content matches; offload scheduled"
                    if local_matches
                    else "current revision remotely verified"
                )
                self._transition(
                    product_path,
                    state,
                    new_state,
                    transition_reason,
                    timestamp,
                    revision=pointer["revision"],
                    local_matches=local_matches,
                )
                state.update(
                    {
                        "product": identity.as_dict(),
                        "current_revision": pointer["revision"],
                        "current_manifest_sha256": manifest_sha256,
                        "current_manifest": manifest,
                        "last_verified_at": utc_text(timestamp),
                        "last_restore_verified_at": utc_text(timestamp),
                        "last_restore_verified_revision": pointer["revision"],
                        "eligible_after": utc_text(
                            timestamp + datetime_module.timedelta(days=self.offload_age_days)
                        ),
                        "last_local_matches": local_matches,
                        "last_local_error": local_error,
                        "last_error": None,
                    }
                )
                save_local_state(product_path, state)
                receipt = self._write_receipt(
                    "verify",
                    identity,
                    state_before,
                    str(state["state"]),
                    {
                        "result": "verified",
                        "revision": pointer["revision"],
                        "manifest_sha256": manifest_sha256,
                        "local_matches": local_matches,
                        "remote_verification": remote_verification,
                    },
                    timestamp,
                )
                state["last_receipt"] = str(receipt)
                save_local_state(product_path, state)
                return {
                    "ok": local_matches,
                    "product": identity.key,
                    "revision": pointer["revision"],
                    "manifest_sha256": manifest_sha256,
                    "local_matches": local_matches,
                    "state": state["state"],
                    "remote_verified": True,
                    "local_error": local_error,
                    "receipt": str(receipt),
                }
            except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError) as exc:
                state["last_error"] = _redact_error(str(exc))
                try:
                    self._transition(product_path, state, "ERROR", "verification failed", timestamp, error=str(exc))
                except CloudAssetError:
                    save_local_state(product_path, state)
                receipt = self._write_receipt(
                    "verify",
                    identity,
                    state_before,
                    str(state.get("state", "ERROR")),
                    {"result": "error", "error": _redact_error(str(exc))},
                    timestamp,
                )
                state["last_receipt"] = str(receipt)
                save_local_state(product_path, state)
                raise

    def status(
        self,
        product_root: Union[str, Path],
        check_remote: bool = False,
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        product_path, identity = self._resolve(product_root)
        timestamp = now or utc_now()
        with ProductLock(product_path, self.lock_timeout_seconds):
            state = load_local_state(product_path, identity)
            local_present = self._local_has_both_content_dirs(product_path)
            local_any = self._local_has_any_content(product_path)
            local_empty_after_offload = self._local_content_dirs_empty(product_path)
            local_matches: Optional[bool] = None
            local_error: Optional[str] = None
            comparison_manifest = state.get("current_manifest")
            comparison_revision = state.get("current_revision")
            comparison_hash = state.get("current_manifest_sha256")
            if not comparison_revision and state.get("candidate_manifest"):
                comparison_manifest = state.get("candidate_manifest")
                comparison_revision = state.get("candidate_revision")
                comparison_hash = state.get("candidate_manifest_sha256")
            if local_present and comparison_manifest and not (
                local_empty_after_offload
                and state.get("state") in {"CLOUD_ONLY", "CLEANUP_PENDING"}
            ):
                try:
                    local_matches = self._local_matches(product_path, comparison_manifest)
                except (AssetValidationError, TypeError, KeyError) as exc:
                    local_matches = False
                    local_error = str(exc)
            if not state.get("current_revision"):
                if state.get("candidate_revision"):
                    desired = "DIRTY_LOCAL"
                else:
                    desired = "LOCAL_ONLY" if local_present else "ERROR"
            elif (
                local_empty_after_offload
                and state.get("state") in {"CLOUD_ONLY", "CLEANUP_PENDING"}
            ):
                desired = state.get("state")
            elif local_matches is False or (local_any and not local_present):
                desired = "DIRTY_LOCAL"
            elif not local_present:
                # Keep a cleanup failure visible instead of silently reducing it
                # to ordinary CLOUD_ONLY.  The immediate offload workflow may
                # retry that quarantine cleanup on the next explicit request.
                desired = "CLEANUP_PENDING" if state.get("state") == "CLEANUP_PENDING" else "CLOUD_ONLY"
            else:
                desired = state.get("state", "CLOUD_VERIFIED")
                if desired in {"LOCAL_ONLY", "ERROR", "DIRTY_LOCAL", "CLOUD_ONLY"}:
                    desired = "CLOUD_VERIFIED"
            if desired != state.get("state"):
                try:
                    self._transition(product_path, state, desired, "local status inspected", timestamp)
                except CloudAssetError:
                    state["state"] = desired
                    save_local_state(product_path, state)
            remote_verified = None
            remote_error = None
            if check_remote and state.get("current_revision"):
                try:
                    pointer, manifest, digest = self._read_current_and_manifest(identity)
                    if local_present and local_matches:
                        with self._local_revision_stage(product_path, manifest) as stage:
                            self._verify_remote_revision(identity, pointer, manifest, stage)
                    else:
                        self._verify_remote_revision(identity, pointer, manifest)
                    remote_verified = True
                    if digest != state.get("current_manifest_sha256"):
                        state["current_manifest"] = manifest
                        state["current_manifest_sha256"] = digest
                        save_local_state(product_path, state)
                except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError) as exc:
                    remote_verified = False
                    remote_error = _redact_error(str(exc))
            local_counts: Dict[str, int] = {}
            if local_present:
                try:
                    local_counts = _counts(_collect_records(product_path))
                except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError) as exc:
                    local_error = local_error or _redact_error(str(exc))
            local_assets_complete = bool(
                local_counts.get("images", 0) and local_counts.get("files", 0)
            )
            cloud_manifest = state.get("current_manifest") if state.get("current_revision") else None
            cloud_counts = dict(cloud_manifest.get("counts") or {}) if isinstance(cloud_manifest, dict) else {}
            candidate_manifest = state.get("candidate_manifest")
            candidate_counts = dict(candidate_manifest.get("counts") or {}) if isinstance(candidate_manifest, dict) else {}
            counts = cloud_counts or candidate_counts or local_counts
            cache_info: Dict[str, Any] = {
                "available": False,
                "status": "none",
                "revision": state.get("current_revision"),
                "expires_at": None,
                "cleanup_eligible_at": None,
                "cache_path": None,
                "last_error": None,
            }
            if state.get("current_revision"):
                try:
                    cache_lookup = self._cached_hydration_lookup(
                        identity,
                        str(state["current_revision"]),
                        str(state.get("current_manifest_sha256") or ""),
                        timestamp,
                    )
                    cache_info = {
                        "available": bool(cache_lookup.get("hit") and cache_lookup.get("status") == "success"),
                        "status": cache_lookup.get("status", "miss"),
                        "revision": state.get("current_revision"),
                        "expires_at": cache_lookup.get("metadata", {}).get("expires_at"),
                        "cleanup_eligible_at": cache_lookup.get("metadata", {}).get("cleanup_eligible_at"),
                        "cache_path": str(cache_lookup.get("cache_path")) if cache_lookup.get("cache_path") else str(self._hydration_cache_path(identity, str(state["current_revision"]))),
                        "last_error": cache_lookup.get("metadata", {}).get("error") if cache_lookup.get("status") == "failure" else cache_lookup.get("error"),
                    }
                except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError) as exc:
                    cache_info["last_error"] = _redact_error(str(exc))
            return {
                "ok": desired not in {"ERROR", "DIRTY_LOCAL", "CLEANUP_PENDING"},
                "product": identity.key,
                "path": str(product_path),
                "scope": identity.scope,
                "shop": identity.shop,
                "product_name": identity.product,
                "state": state.get("state", desired),
                "current_revision": state.get("current_revision"),
                "current_manifest_sha256": state.get("current_manifest_sha256"),
                "revision": comparison_revision,
                "hash": comparison_hash,
                "candidate_revision": state.get("candidate_revision"),
                "candidate_manifest_sha256": state.get("candidate_manifest_sha256"),
                "cloud_revision": state.get("cloud_revision") or state.get("current_revision"),
                "local_present": local_present,
                "local_matches": local_matches,
                "local_available": bool(local_counts.get("total_bytes", 0)),
                "local_assets_complete": local_assets_complete,
                "cloud_available": bool(state.get("current_revision") or state.get("cloud_revision")),
                "cache_available": cache_info["available"],
                "local_error": local_error,
                "remote_verified": remote_verified,
                "remote_error": remote_error,
                "counts": counts,
                "local_counts": local_counts,
                "cloud_counts": cloud_counts,
                "bytes": int(counts.get("total_bytes", 0) or 0),
                "reclaimable_bytes": int(local_counts.get("total_bytes", 0) or 0) if state.get("eligible_after") else 0,
                "preview_available": bool(
                    (product_path / PREVIEW_FILE_NAME).is_file()
                    or any(record.get("role") == "preview" for record in (cloud_manifest or {}).get("files", []))
                ),
                "cache": cache_info,
                "last_restore_verified_at": state.get("last_restore_verified_at"),
                "eligible_after": state.get("eligible_after"),
                "last_error": state.get("last_error"),
            }

    def restore(
        self,
        product_root: Union[str, Path],
        force: bool = False,
        now: Optional[datetime_module.datetime] = None,
    ) -> Dict[str, Any]:
        product_path, identity = self._resolve(product_root)
        timestamp = now or utc_now()
        with ProductLock(product_path, self.lock_timeout_seconds):
            state = load_local_state(product_path, identity)
            state_before = str(state.get("state", "LOCAL_ONLY"))
            try:
                pointer, manifest, manifest_sha256 = self._read_current_and_manifest(identity)
                local_any = self._local_has_any_content(product_path)
                if local_any and not force:
                    try:
                        if not self._local_matches(product_path, manifest):
                            raise CloudAssetError(
                                "local assets are dirty; use --force to restore over them"
                            )
                    except AssetValidationError as exc:
                        raise CloudAssetError(
                            "local assets are incomplete; use --force to restore them"
                        ) from exc
                self._transition(product_path, state, "RESTORING", "restore started", timestamp, revision=pointer["revision"])
                restore_parent = product_path.parent
                with self._remote_restore_stage(
                    identity,
                    str(pointer["revision"]),
                    prefix=f".{product_path.name}.cloud-restore-",
                    parent=restore_parent,
                ) as stage:
                    manifest_path = stage / "manifest.json"
                    _validate_regular_file(manifest_path)
                    downloaded_manifest, downloaded_digest = self._decode_manifest(
                        manifest_path.read_bytes(), manifest_sha256
                    )
                    verify_manifest_directory(stage, downloaded_manifest)
                    self.remote.verify_directory(stage, self._revision_path(identity, str(pointer["revision"])))
                    self._transition(
                        product_path,
                        state,
                        "RESTORE_VERIFIED",
                        "remote revision downloaded and restore staged bytes verified",
                        timestamp,
                        revision=pointer["revision"],
                    )
                    self._install_restored_assets(product_path, stage, downloaded_manifest)
                if not self._local_matches(product_path, manifest):
                    raise AssetValidationError("restored local assets failed manifest verification")
                self._transition(
                    product_path,
                    state,
                    "READY_LOCAL",
                    "restored local assets verified",
                    timestamp,
                    revision=pointer["revision"],
                )
                state.update(
                    {
                        "product": identity.as_dict(),
                        "current_revision": pointer["revision"],
                        "current_manifest_sha256": manifest_sha256,
                        "current_manifest": manifest,
                        "last_restore_verified_at": utc_text(timestamp),
                        "last_restore_verified_revision": pointer["revision"],
                        "eligible_after": utc_text(
                            timestamp + datetime_module.timedelta(days=self.offload_age_days)
                        ),
                        "last_local_matches": True,
                        "last_error": None,
                    }
                )
                save_local_state(product_path, state)
                receipt = self._write_receipt(
                    "restore",
                    identity,
                    state_before,
                    str(state["state"]),
                    {
                        "result": "restored",
                        "revision": pointer["revision"],
                        "manifest_sha256": downloaded_digest,
                        "force": force,
                        "restore_verified_before_local_install": True,
                    },
                    timestamp,
                )
                state["last_receipt"] = str(receipt)
                save_local_state(product_path, state)
                return {
                    "ok": True,
                    "product": identity.key,
                    "revision": pointer["revision"],
                    "manifest_sha256": manifest_sha256,
                    "state": state["state"],
                    "receipt": str(receipt),
                }
            except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError) as exc:
                state["last_error"] = _redact_error(str(exc))
                try:
                    self._transition(product_path, state, "ERROR", "restore failed", timestamp, error=str(exc))
                except CloudAssetError:
                    save_local_state(product_path, state)
                receipt = self._write_receipt(
                    "restore",
                    identity,
                    state_before,
                    str(state.get("state", "ERROR")),
                    {"result": "error", "error": _redact_error(str(exc)), "force": force},
                    timestamp,
                )
                state["last_receipt"] = str(receipt)
                save_local_state(product_path, state)
                raise

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink():
            raise AssetValidationError(f"symlinks are not allowed during offload: {path}")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def _clear_full_asset_contents(self, product_root: Path) -> None:
        for dirname in CONTENT_DIRS:
            directory = product_root / dirname
            _ensure_directory(directory, dirname)
            for child in sorted(directory.iterdir(), key=lambda item: item.name):
                if child.is_symlink():
                    raise AssetValidationError(f"symlinks are not allowed during offload: {child}")
                if child.is_dir():
                    for current, dirnames, filenames in os.walk(child, followlinks=False):
                        current_path = Path(current)
                        if any((current_path / name).is_symlink() for name in dirnames + filenames):
                            raise AssetValidationError(f"symlink found during offload: {current_path}")
                self._remove_path(child)

    def _install_restored_assets(self, product_root: Path, stage: Path, manifest: Mapping[str, Any]) -> None:
        records = _records_from_manifest(manifest)
        for dirname in CONTENT_DIRS:
            _ensure_directory(stage / dirname, dirname)
        expected_dirs = {record.path.split("/", 1)[0] for record in records if record.role in {"image", "file"}}
        if set(CONTENT_DIRS) != expected_dirs:
            raise AssetValidationError("restore manifest is missing images or files")
        backup = product_root.parent / f".{product_root.name}.cloud-restore-backup-{uuid.uuid4().hex}"
        backup.mkdir()
        moved_existing: List[str] = []
        installed: List[str] = []
        try:
            for name in (*CONTENT_DIRS, PREVIEW_FILE_NAME):
                existing = product_root / name
                if existing.exists() or existing.is_symlink():
                    os.replace(existing, backup / name)
                    moved_existing.append(name)
            for name in CONTENT_DIRS:
                os.replace(stage / name, product_root / name)
                installed.append(name)
            preview_source = stage / PREVIEW_FILE_NAME
            if preview_source.exists() or preview_source.is_symlink():
                os.replace(preview_source, product_root / PREVIEW_FILE_NAME)
                installed.append(PREVIEW_FILE_NAME)
            shutil.rmtree(backup, ignore_errors=True)
        except (OSError, AssetValidationError):
            for name in installed:
                self._remove_path(product_root / name)
            for name in reversed(moved_existing):
                backup_item = backup / name
                if backup_item.exists() or backup_item.is_symlink():
                    os.replace(backup_item, product_root / name)
            shutil.rmtree(backup, ignore_errors=True)
            raise

    def maintain(
        self,
        product_roots: Optional[Sequence[Union[str, Path]]] = None,
        apply: bool = False,
        offload_enabled: bool = False,
        allowlist: Sequence[str] = (),
        older_than_days: Optional[int] = None,
        now: Optional[datetime_module.datetime] = None,
    ) -> List[Dict[str, Any]]:
        requested_age_days = self.offload_age_days if older_than_days is None else int(older_than_days)
        if requested_age_days < DEFAULT_OFFLOAD_AGE_DAYS:
            raise CloudAssetError(
                f"maintenance eligibility cannot be less than {DEFAULT_OFFLOAD_AGE_DAYS} days"
            )
        # An invocation may request a longer soak, but it cannot weaken the
        # configured policy age that was used to write eligible_after.
        eligibility_age_days = max(self.offload_age_days, requested_age_days)
        targets = list(product_roots) if product_roots is not None else [
            path for path, _ in discover_product_roots(self.repo_root)
        ]
        allowed = {str(value).strip() for value in allowlist if str(value).strip()}
        timestamp = now or utc_now()
        results: List[Dict[str, Any]] = []
        for target in targets:
            try:
                product_path, identity = self._resolve(target)
            except (AssetValidationError, OSError) as exc:
                results.append({"ok": False, "path": str(target), "reason": _redact_error(str(exc))})
                continue
            with ProductLock(product_path, self.lock_timeout_seconds):
                state = load_local_state(product_path, identity)
                state_before = str(state.get("state", "LOCAL_ONLY"))
                details: Dict[str, Any] = {
                    "apply": apply,
                    "offload_enabled": offload_enabled,
                    "allowlisted": identity.key in allowed,
                    "older_than_days": eligibility_age_days,
                    "configured_offload_age_days": self.offload_age_days,
                    "checks": {},
                }
                try:
                    if not offload_enabled:
                        details["reason"] = "offload policy disabled"
                        result = self._maintenance_skip(
                            product_path,
                            identity,
                            state,
                            state_before,
                            details,
                            timestamp,
                            persist_state=apply,
                        )
                        results.append(result)
                        continue
                    if identity.key not in allowed:
                        details["reason"] = "product is not in explicit offload allowlist"
                        result = self._maintenance_skip(
                            product_path,
                            identity,
                            state,
                            state_before,
                            details,
                            timestamp,
                            persist_state=apply,
                        )
                        results.append(result)
                        continue
                    if not state.get("current_revision"):
                        details["reason"] = "no cloud revision"
                        result = self._maintenance_skip(
                            product_path,
                            identity,
                            state,
                            state_before,
                            details,
                            timestamp,
                            persist_state=apply,
                        )
                        results.append(result)
                        continue
                    restore_verified_at = parse_utc(state.get("last_restore_verified_at"))
                    stored_eligible_after = parse_utc(state.get("eligible_after"))
                    configured_eligible_after = (
                        restore_verified_at
                        + datetime_module.timedelta(days=eligibility_age_days)
                        if restore_verified_at
                        else None
                    )
                    age_ok = bool(
                        restore_verified_at
                        and stored_eligible_after
                        and configured_eligible_after
                        and timestamp >= stored_eligible_after
                        and timestamp >= configured_eligible_after
                    )
                    details["checks"]["last_restore_verified_at"] = (
                        utc_text(restore_verified_at) if restore_verified_at else None
                    )
                    details["checks"]["eligible_after"] = (
                        utc_text(stored_eligible_after) if stored_eligible_after else None
                    )
                    details["checks"]["configured_eligible_after"] = (
                        utc_text(configured_eligible_after) if configured_eligible_after else None
                    )
                    details["checks"]["age_ok"] = age_ok
                    if not age_ok:
                        details["reason"] = (
                            "no successful temporary restore verification has completed the eligibility window"
                            if not restore_verified_at or not stored_eligible_after
                            else "restore verification is younger than the eligibility window"
                        )
                        result = self._maintenance_skip(
                            product_path,
                            identity,
                            state,
                            state_before,
                            details,
                            timestamp,
                            persist_state=apply,
                        )
                        results.append(result)
                        continue
                    pointer, manifest, manifest_sha256 = self._read_current_and_manifest(identity)
                    same_revision = pointer.get("revision") == state.get("current_revision")
                    same_manifest = manifest_sha256 == state.get("current_manifest_sha256")
                    details["checks"]["current_pointer_matches_state"] = same_revision and same_manifest
                    if not same_revision or not same_manifest:
                        raise RemoteStoreError("current.json changed since local state was recorded")
                    if not self._local_has_both_content_dirs(product_path):
                        raise AssetValidationError("local images/files are not both present for offload")
                    local_matches = self._local_matches(product_path, manifest)
                    details["checks"]["local_hash_matches_manifest"] = local_matches
                    if not local_matches:
                        if apply:
                            self._transition(
                                product_path,
                                state,
                                "DIRTY_LOCAL",
                                "maintenance found local changes",
                                timestamp,
                            )
                        details["reason"] = "local content no longer matches current manifest"
                        result = self._maintenance_skip(
                            product_path,
                            identity,
                            state,
                            state_before,
                            details,
                            timestamp,
                            persist_state=apply,
                        )
                        results.append(result)
                        continue
                    with self._local_revision_stage(product_path, manifest) as local_stage:
                        remote_verification = self._verify_remote_revision(identity, pointer, manifest, local_stage)
                    details["checks"]["remote_reverification"] = remote_verification
                    with self._remote_restore_stage(
                        identity,
                        str(pointer["revision"]),
                        prefix=f".{product_path.name}.cloud-maintenance-restore-",
                        parent=product_path.parent,
                    ) as restore_stage:
                        manifest_path = restore_stage / "manifest.json"
                        _validate_regular_file(manifest_path)
                        downloaded_manifest, downloaded_digest = self._decode_manifest(
                            manifest_path.read_bytes(), manifest_sha256
                        )
                        verify_manifest_directory(restore_stage, downloaded_manifest)
                        self.remote.verify_directory(restore_stage, self._revision_path(identity, str(pointer["revision"])))
                        details["checks"]["restore_verified"] = True
                        details["checks"]["restore_manifest_sha256"] = downloaded_digest
                    if not apply:
                        receipt = self._write_receipt(
                            "maintain",
                            identity,
                            state_before,
                            state_before,
                            {**details, "result": "dry_run_would_offload"},
                            timestamp,
                        )
                        results.append(
                            {
                                "ok": True,
                                "product": identity.key,
                                "revision": pointer["revision"],
                                "state": state_before,
                                "would_offload": True,
                                "applied": False,
                                "receipt": str(receipt),
                            }
                        )
                        continue
                    self._transition(
                        product_path,
                        state,
                        "OFFLOAD_SCHEDULED",
                        "eligible maintenance candidate passed all pre-delete checks",
                        timestamp,
                        revision=pointer["revision"],
                    )
                    self._transition(
                        product_path,
                        state,
                        "RESTORE_VERIFIED",
                        "maintenance restore verification passed before offload",
                        timestamp,
                        revision=pointer["revision"],
                    )
                    # Recheck the local hash immediately before deletion while the product lock is held.
                    if not self._local_matches(product_path, manifest):
                        raise AssetValidationError("local content changed before offload deletion")
                    self._clear_full_asset_contents(product_path)
                    self._transition(
                        product_path,
                        state,
                        "CLOUD_ONLY",
                        "full image and file contents offloaded after restore verification",
                        timestamp,
                        revision=pointer["revision"],
                    )
                    state.update(
                        {
                            "offloaded_at": utc_text(timestamp),
                            "last_local_matches": False,
                            "last_error": None,
                        }
                    )
                    save_local_state(product_path, state)
                    receipt = self._write_receipt(
                        "maintain",
                        identity,
                        state_before,
                        str(state["state"]),
                        {**details, "result": "offloaded", "deleted": ["images/*", "files/*"]},
                        timestamp,
                    )
                    state["last_receipt"] = str(receipt)
                    save_local_state(product_path, state)
                    results.append(
                        {
                            "ok": True,
                            "product": identity.key,
                            "revision": pointer["revision"],
                            "state": state["state"],
                            "would_offload": False,
                            "applied": True,
                            "receipt": str(receipt),
                        }
                    )
                except (AssetValidationError, CloudAssetError, OSError, ValueError, TypeError, KeyError) as exc:
                    details["reason"] = _redact_error(str(exc))
                    if apply:
                        state["last_error"] = details["reason"]
                        try:
                            self._transition(
                                product_path,
                                state,
                                "ERROR",
                                "maintenance checks failed",
                                timestamp,
                                error=str(exc),
                            )
                        except CloudAssetError:
                            save_local_state(product_path, state)
                    receipt = self._write_receipt(
                        "maintain",
                        identity,
                        state_before,
                        str(state.get("state", state_before)) if apply else state_before,
                        {**details, "result": "blocked"},
                        timestamp,
                    )
                    if apply:
                        state["last_receipt"] = str(receipt)
                        save_local_state(product_path, state)
                    results.append(
                        {
                            "ok": False,
                            "product": identity.key,
                            "state": state.get("state", state_before) if apply else state_before,
                            "reason": details["reason"],
                            "would_offload": False,
                            "applied": False,
                            "receipt": str(receipt),
                        }
                    )
        return results

    def _maintenance_skip(
        self,
        product_path: Path,
        identity: ProductIdentity,
        state: Dict[str, Any],
        state_before: str,
        details: Dict[str, Any],
        timestamp: datetime_module.datetime,
        persist_state: bool = True,
    ) -> Dict[str, Any]:
        receipt = self._write_receipt(
            "maintain",
            identity,
            state_before,
            str(state.get("state", state_before)),
            {**details, "result": "skipped"},
            timestamp,
        )
        if persist_state:
            state["last_receipt"] = str(receipt)
            save_local_state(product_path, state)
        return {
            "ok": True,
            "product": identity.key,
            "state": state.get("state", state_before),
            "would_offload": False,
            "applied": False,
            "reason": details.get("reason"),
            "receipt": str(receipt),
        }


def discover_product_roots(repo_root: Path) -> List[Tuple[Path, ProductIdentity]]:
    """Inventory only the canonical master and per-shop product layouts."""

    root = Path(repo_root).absolute()
    found: List[Tuple[Path, ProductIdentity]] = []
    master_root = root / "master_products"
    if master_root.is_dir() and not master_root.is_symlink():
        for child in sorted(master_root.iterdir(), key=lambda item: item.name):
            if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
                continue
            try:
                found.append(resolve_product(root, child))
            except AssetValidationError:
                continue
    shops_root = root / "shops"
    if shops_root.is_dir() and not shops_root.is_symlink():
        for shop in sorted(shops_root.iterdir(), key=lambda item: item.name):
            if shop.name.startswith(".") or not shop.is_dir() or shop.is_symlink():
                continue
            for child in sorted(shop.iterdir(), key=lambda item: item.name):
                if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
                    continue
                try:
                    found.append(resolve_product(root, child))
                except AssetValidationError:
                    continue
    return found


__all__ = [
    "AssetValidationError",
    "CacheLookup",
    "CloudAssetError",
    "CloudAssetStore",
    "DEFAULT_HYDRATION_PURPOSE",
    "HydrationCache",
    "LocalRemote",
    "ProductIdentity",
    "ProductLock",
    "RemoteConflictError",
    "RemoteStore",
    "RemoteStoreError",
    "RcloneRemote",
    "STATES",
    "build_manifest",
    "canonical_json_bytes",
    "load_local_state",
    "parse_utc",
    "resolve_product",
    "save_local_state",
    "sha256_bytes",
    "sha256_file",
    "transition_state",
    "utc_now",
    "utc_text",
    "verify_manifest_directory",
]
