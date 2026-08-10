"""Configuration for the Phase 1 cloud asset store.

The configuration intentionally contains only routing and policy values.  OAuth
credentials remain in rclone's own configuration and are never read, copied,
or serialized by this project.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


DEFAULT_REMOTE = "gdrive_dest"
DEFAULT_PARENT_ID = "1cg5xsQ_3HIPEDASOco9MddHrm993DoCA"
DEFAULT_RCLONE_BIN = "/opt/homebrew/bin/rclone"
DEFAULT_CONFIG_NAME = "cloud_asset_store.config.json"
DEFAULT_CACHE_RELATIVE = Path("output") / "cloud-cache"
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
DEFAULT_SUCCESS_TTL_SECONDS = 24 * 60 * 60
# Failed hydration/operation metadata is retained for up to the approved
# seven-day retry window, while callers may still provide a shorter test or
# environment-specific TTL explicitly.
DEFAULT_FAILURE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_OFFLOAD_AGE_DAYS = 7


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _allowlist(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ValueError("offload allowlist must be a string or list")
    cleaned = []
    for item in values:
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return tuple(dict.fromkeys(cleaned))


@dataclass(frozen=True)
class CloudAssetConfig:
    """Resolved, non-secret cloud asset store configuration."""

    repo_root: Path
    remote: str = DEFAULT_REMOTE
    parent_id: str = DEFAULT_PARENT_ID
    rclone_bin: str = DEFAULT_RCLONE_BIN
    cache_root: Path = field(default_factory=lambda: DEFAULT_CACHE_RELATIVE)
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS
    success_ttl_seconds: int = DEFAULT_SUCCESS_TTL_SECONDS
    failure_ttl_seconds: int = DEFAULT_FAILURE_TTL_SECONDS
    offload_enabled: bool = False
    offload_allowlist: Tuple[str, ...] = ()
    offload_age_days: int = DEFAULT_OFFLOAD_AGE_DAYS

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_root", Path(self.repo_root).expanduser().absolute())
        cache = Path(self.cache_root).expanduser()
        if not cache.is_absolute():
            cache = self.repo_root / cache
        object.__setattr__(self, "cache_root", cache.absolute())
        if self.lock_timeout_seconds <= 0:
            raise ValueError("lock_timeout_seconds must be positive")
        if self.success_ttl_seconds <= 0 or self.failure_ttl_seconds <= 0:
            raise ValueError("cache TTLs must be positive")
        if self.offload_age_days < 7:
            raise ValueError("offload_age_days cannot be less than 7")

    def public_dict(self) -> Dict[str, Any]:
        """Return the safe subset suitable for logs and audit receipts."""

        return {
            "repo_root": str(self.repo_root),
            "remote": self.remote,
            "parent_id": self.parent_id,
            "rclone_bin": self.rclone_bin,
            "cache_root": str(self.cache_root),
            "lock_timeout_seconds": self.lock_timeout_seconds,
            "success_ttl_seconds": self.success_ttl_seconds,
            "failure_ttl_seconds": self.failure_ttl_seconds,
            "offload_enabled": self.offload_enabled,
            "offload_allowlist": list(self.offload_allowlist),
            "offload_age_days": self.offload_age_days,
        }


def _file_config(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"cloud asset config must be a JSON object: {path}")
    return value


def load_config(repo_root: Optional[Path] = None, path: Optional[Path] = None) -> CloudAssetConfig:
    """Load file settings, then apply environment overrides.

    A missing config file is valid and uses the existing backup remote and
    parent ID.  The default offload policy is deliberately disabled.
    """

    root = Path(repo_root or Path(__file__).resolve().parent).expanduser().absolute()
    config_path = Path(path).expanduser() if path else root / DEFAULT_CONFIG_NAME
    if not config_path.is_absolute():
        config_path = root / config_path
    raw = _file_config(config_path)
    offload = raw.get("offload", {})
    if offload is None:
        offload = {}
    if not isinstance(offload, dict):
        raise ValueError("cloud asset offload config must be an object")

    remote = os.environ.get("ETSY_CLOUD_RCLONE_REMOTE", raw.get("remote", DEFAULT_REMOTE))
    parent_id = os.environ.get(
        "ETSY_CLOUD_DRIVE_PARENT_ID",
        raw.get("parent_id", raw.get("drive_parent_id", DEFAULT_PARENT_ID)),
    )
    rclone_bin = os.environ.get(
        "ETSY_CLOUD_RCLONE_BIN", raw.get("rclone_bin", DEFAULT_RCLONE_BIN)
    )
    cache_root = os.environ.get("ETSY_CLOUD_CACHE_ROOT", raw.get("cache_root", DEFAULT_CACHE_RELATIVE))

    allowlist_value = os.environ.get(
        "ETSY_CLOUD_OFFLOAD_ALLOWLIST",
        offload.get("allowlist", raw.get("offload_allowlist", ())),
    )
    enabled = _env_bool(
        "ETSY_CLOUD_OFFLOAD_ENABLED",
        bool(offload.get("enabled", raw.get("offload_enabled", False))),
    )
    age_days = _env_int(
        "ETSY_CLOUD_OFFLOAD_AGE_DAYS",
        int(offload.get("age_days", raw.get("offload_age_days", DEFAULT_OFFLOAD_AGE_DAYS))),
    )

    return CloudAssetConfig(
        repo_root=root,
        remote=str(remote),
        parent_id=str(parent_id),
        rclone_bin=str(rclone_bin),
        cache_root=Path(cache_root),
        lock_timeout_seconds=_env_float(
            "ETSY_CLOUD_LOCK_TIMEOUT_SECONDS",
            float(raw.get("lock_timeout_seconds", DEFAULT_LOCK_TIMEOUT_SECONDS)),
        ),
        success_ttl_seconds=_env_int(
            "ETSY_CLOUD_CACHE_SUCCESS_TTL_SECONDS",
            int(raw.get("success_ttl_seconds", DEFAULT_SUCCESS_TTL_SECONDS)),
        ),
        failure_ttl_seconds=_env_int(
            "ETSY_CLOUD_CACHE_FAILURE_TTL_SECONDS",
            int(raw.get("failure_ttl_seconds", DEFAULT_FAILURE_TTL_SECONDS)),
        ),
        offload_enabled=enabled,
        offload_allowlist=_allowlist(allowlist_value),
        offload_age_days=age_days,
    )


__all__ = ["CloudAssetConfig", "load_config"]
