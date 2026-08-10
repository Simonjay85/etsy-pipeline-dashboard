"""Persistent, shop-isolated social publication records."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except Exception:  # pragma: no cover - fallback for non-POSIX test runners
    fcntl = None

STORE_FILENAME = "social_post_status.json"
LEGACY_STORE_FILENAME = "social_posts.json"
STORE_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_id(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or not _SAFE_ID.fullmatch(normalized):
        raise ValueError(f"{label} không hợp lệ")
    return normalized


def social_post_store_path(base_dir: Path, shop_id: str) -> Path:
    safe_shop = _validate_id(shop_id, "shop_id")
    return Path(base_dir) / "shops" / safe_shop / STORE_FILENAME


def legacy_social_post_store_path(base_dir: Path, shop_id: str) -> Path:
    safe_shop = _validate_id(shop_id, "shop_id")
    return Path(base_dir) / "shops" / safe_shop / LEGACY_STORE_FILENAME


def _coerce_isotimestamp(value: str | None) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    candidate = str(value).strip()
    if not candidate:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _empty_store(shop_id: str) -> dict:
    return {
        "version": STORE_VERSION,
        "shop_id": shop_id,
        "products": {},
    }


def _read_store(path: Path, shop_id: str) -> dict:
    if not path.exists():
        legacy_path = path.with_name(LEGACY_STORE_FILENAME)
        if legacy_path.exists():
            path = legacy_path
    if not path.exists():
        return _empty_store(shop_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_store(shop_id)
    if not isinstance(data, dict) or data.get("shop_id") != shop_id:
        return _empty_store(shop_id)
    if not isinstance(data.get("products"), dict):
        data["products"] = {}
    data["version"] = STORE_VERSION
    return data


def load_social_post_records(base_dir: Path, shop_id: str) -> dict:
    path = social_post_store_path(base_dir, shop_id)
    return _read_store(path, shop_id)


def get_product_social_statuses(
    base_dir: Path,
    shop_id: str,
    folder: str,
    row: int | None = None,
) -> dict:
    safe_folder = _validate_id(folder, "folder")
    products = load_social_post_records(base_dir, shop_id).get("products", {})
    product = products.get(safe_folder)
    if not isinstance(product, dict) and row is not None:
        product = next(
            (
                candidate
                for candidate in products.values()
                if isinstance(candidate, dict) and candidate.get("row") == int(row)
            ),
            None,
        )
    channels = product.get("channels", {}) if isinstance(product, dict) else {}
    return channels if isinstance(channels, dict) else {}


def record_social_post(
    base_dir: Path,
    shop_id: str,
    folder: str,
    row: int,
    platform: str,
    *,
    url: str | None = None,
    detail: str | None = None,
    posted_at: str | None = None,
    source: str = "social_auto_post",
) -> dict:
    safe_shop = _validate_id(shop_id, "shop_id")
    safe_folder = _validate_id(folder, "folder")
    safe_platform = _validate_id(platform.lower(), "platform")
    path = social_post_store_path(base_dir, safe_shop)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")

    record = {
        "status": "posted",
        "posted_at": _coerce_isotimestamp(posted_at),
        "url": str(url or "").strip(),
        "detail": str(detail or "").strip(),
        "source": str(source or "social_auto_post").strip(),
    }

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        data = _read_store(path, safe_shop)
        product = data["products"].setdefault(
            safe_folder,
            {"folder": safe_folder, "row": int(row), "channels": {}},
        )
        product["folder"] = safe_folder
        product["row"] = int(row)
        if not isinstance(product.get("channels"), dict):
            product["channels"] = {}
        product["channels"][safe_platform] = record

        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{STORE_FILENAME}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                json.dump(data, temp_file, indent=2, ensure_ascii=False)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, path)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return record
