"""Local validation helpers for Etsy runtime shops config.

Keep this module independent from `dashboard_app.py` so it is directly
testable without importing the live dashboard runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


DEFAULT_CONFIG_NAME = "shops_config.json"
DEFAULT_EXAMPLE_NAME = "shops_config.example.json"
DIAGNOSTICS_DIR_NAME = ".etsy-dashboard-diagnostics"
REQUIRED_SHOP_FIELDS = (
    "id",
    "name",
    "emoji",
    "etsy_link",
    "social_links",
    "shop_info",
)
REQUIRED_IDENTITY_FIELDS = ("id", "name", "emoji", "etsy_link")
OPTIONAL_SHOP_FIELDS = ("browser_session",)


class ShopsConfigError(ValueError):
    """Raised when `shops_config.json` is missing or malformed."""


def resolve_config_path(config_path: str | Path, base_dir: str | Path | None = None) -> Path:
    """Return an absolute config path resolved against a base directory."""

    base = Path(base_dir or Path.cwd()).resolve()
    path = Path(config_path)
    if not path.is_absolute():
        path = base / path
    return path


def diagnostics_dir(base_dir: str | Path | None = None) -> Path:
    """Return the canonical diagnostics directory used by runtime helpers."""

    return Path(base_dir or Path.cwd()).expanduser().resolve() / DIAGNOSTICS_DIR_NAME


def _validate_shop_name(shop_id: str) -> str:
    if not isinstance(shop_id, str) or not shop_id.strip():
        raise ShopsConfigError("Shop keys must be non-empty strings.")
    return shop_id.strip()


def _validate_shop_block(shop_id: str, shop_block: Any) -> dict[str, Any]:
    if not isinstance(shop_block, Mapping):
        raise ShopsConfigError(f"Shop '{shop_id}' must be an object.")

    block = dict(shop_block)
    for field in REQUIRED_SHOP_FIELDS:
        if field not in block:
            raise ShopsConfigError(f"Shop '{shop_id}' is missing required field '{field}'.")
        value = block[field]
        if not isinstance(value, str):
            raise ShopsConfigError(f"Shop '{shop_id}' field '{field}' must be a string.")
        if field in REQUIRED_IDENTITY_FIELDS and not value.strip():
            raise ShopsConfigError(f"Shop '{shop_id}' field '{field}' must be a non-empty string.")

    if block["id"] != shop_id:
        raise ShopsConfigError(f"Shop '{shop_id}' has mismatched id value.")

    normalized: dict[str, Any] = {}

    for field in REQUIRED_SHOP_FIELDS:
        value = block.pop(field)
        normalized[field] = str(value).strip()

    session_value = block.pop("browser_session", "")
    if not isinstance(session_value, str):
        raise ShopsConfigError(
            f"Shop '{shop_id}' field 'browser_session' must be a string when provided."
        )
    normalized["browser_session"] = session_value.strip()

    # Preserve any extra runtime fields (for example legacy/custom metadata).
    normalized.update(block)
    return normalized


def validate_shops_config(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate `shops_config.json` payload and return normalized values."""

    if not isinstance(raw, Mapping):
        raise ShopsConfigError("shops_config.json must be a JSON object at the top level.")

    if not raw:
        raise ShopsConfigError("shops_config.json must define at least one shop.")

    normalized: dict[str, dict[str, Any]] = {}
    for shop_id, shop_block in raw.items():
        normalized_shop_id = _validate_shop_name(shop_id)
        normalized[normalized_shop_id] = _validate_shop_block(normalized_shop_id, shop_block)

    return normalized


def load_shops_config(
    config_path: str | Path = DEFAULT_CONFIG_NAME,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load and validate local shops config from disk.

    This intentionally returns a focused, masked error for missing files so callers
    can print actionable guidance without exposing any config values.
    """

    path = resolve_config_path(config_path, base_dir=base_dir)
    if not path.exists():
        raise FileNotFoundError(
            "Missing required local config file: shops_config.json. "
            f"Copy {DEFAULT_EXAMPLE_NAME} to {DEFAULT_CONFIG_NAME} and fill it before startup."
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShopsConfigError(f"Cannot read {DEFAULT_CONFIG_NAME}: invalid JSON format.") from exc

    return validate_shops_config(raw)


__all__ = [
    "DEFAULT_CONFIG_NAME",
    "DEFAULT_EXAMPLE_NAME",
    "DIAGNOSTICS_DIR_NAME",
    "ShopsConfigError",
    "diagnostics_dir",
    "load_shops_config",
    "resolve_config_path",
    "validate_shops_config",
]
