"""Runtime identity helpers for the Etsy dashboard."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


CANONICAL_ROOT_DEFAULT = Path("/Users/aaronnguyen/Developer/Etsy")
CANONICAL_ROOT_ENV = "ETSY_DASHBOARD_CANONICAL_ROOT"
DEVELOPMENT_OVERRIDE_ENV = "ETSY_DASHBOARD_ALLOW_NON_CANONICAL"

BACKUP_LABEL_DAILY = "com.user.etsy-backup.daily"
BACKUP_LABEL_WEEKLY = "com.user.etsy-backup.weekly"
SOURCE_FILES = (
    "dashboard_app.py",
    "dashboard_static/index.html",
    "dashboard_static/style.css",
    "dashboard_static/app.js",
)


def _is_truthy(name: str) -> bool:
    """Return True for common truthy environment values."""
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on", "y"}


def canonical_root() -> Path:
    """Resolve the canonical dashboard root path.

    `ETSY_DASHBOARD_CANONICAL_ROOT` can override the default in local-only
    development workflows. Production should keep this unset.
    """
    return Path(os.environ.get(CANONICAL_ROOT_ENV, str(CANONICAL_ROOT_DEFAULT))).resolve()


def current_runtime_root(module_file: str | os.PathLike[str]) -> Path:
    """Return the runtime root for the caller's Python module."""
    return Path(module_file).resolve().parent


def is_canonical_runtime(current_root: Path | os.PathLike[str]) -> bool:
    """Return True when current runtime is running from canonical checkout."""
    return Path(current_root).resolve() == canonical_root()


def startup_guard_message(current_root: Path | os.PathLike[str]) -> str | None:
    """Return a refusal message when startup should stop, otherwise None."""
    if is_canonical_runtime(current_root):
        return None
    if _is_truthy(DEVELOPMENT_OVERRIDE_ENV):
        return None
    return (
        "Refusing to start from non-canonical checkout. "
        f"Set {DEVELOPMENT_OVERRIDE_ENV}=1 to allow development override."
    )


def _run_git(*args: str, cwd: Path) -> str | None:
    try:
        return (
            subprocess.check_output(
                ["git", "-C", str(cwd), *args], stderr=subprocess.DEVNULL
            )
            .decode("utf-8", errors="replace")
            .strip()
        )
    except Exception:
        return None


def _sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact_path(raw: str, root: Path) -> str:
    safe = raw.replace(str(root), "<repo-root>")
    return safe.replace(str(Path.home()), "~")


def source_identity(base_dir: Path) -> dict[str, Any]:
    """Return commit/hash identity for runtime source files."""
    commit = _run_git("rev-parse", "--short", "HEAD", cwd=base_dir)
    dirty = None
    if commit is not None:
        status = _run_git("status", "--porcelain", "--untracked-files=no", cwd=base_dir)
        dirty = bool(status)
    else:
        status = None

    file_hashes = {name: _sha256(base_dir / name) for name in SOURCE_FILES}
    return {
        "commit": commit,
        "dirty": dirty,
        "status": status,
        "file_hashes": file_hashes,
    }


def _read_launchctl_loaded_labels() -> set[str]:
    try:
        output = subprocess.check_output(
            ["launchctl", "list"], stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="replace")
    except Exception:
        return set()

    labels: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            labels.add(parts[-1])
    return labels


def _parse_backup_log(log_path: Path, *, root: Path) -> dict[str, Any] | None:
    if not log_path.exists():
        return None

    timestamp_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    last_success = None
    last_failure = None

    for line in reversed(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
        clean = line.strip()
        lower = clean.lower()

        if last_failure is None and ("failed:" in lower or "error:" in lower):
            match = timestamp_re.match(clean)
            last_failure = {
                "timestamp": match.group(0) if match else None,
                "status": "failure",
            }

        if last_success is None and re.search(r"\b(uploaded|completed backup|backup success|snapshot created)\b", lower):
            match = timestamp_re.match(clean)
            last_success = {
                "timestamp": match.group(0) if match else None,
                "status": "success",
            }

        if last_failure is not None and last_success is not None:
            break

    if last_success is None and last_failure is None:
        return None

    return {
        "last_success": last_success,
        "last_failure": last_failure,
    }


def _parse_iso_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _backup_failure_is_current(last_success: Mapping[str, Any] | None, last_failure: Mapping[str, Any] | None) -> bool:
    """Return True when backup failure appears newer than last known success."""
    if not isinstance(last_failure, Mapping):
        return False
    failure_time = _parse_iso_timestamp(last_failure.get("timestamp"))
    if failure_time is None:
        return True
    if not isinstance(last_success, Mapping):
        return True
    success_time = _parse_iso_timestamp(last_success.get("timestamp"))
    if success_time is None:
        return True
    return failure_time > success_time


def backup_scheduler_identity(root: Path) -> dict[str, Any]:
    loaded_labels = _read_launchctl_loaded_labels()
    loaded_daily = BACKUP_LABEL_DAILY in loaded_labels
    loaded_weekly = BACKUP_LABEL_WEEKLY in loaded_labels
    daily_plist = root / "com.user.etsy-backup.daily.plist"
    weekly_plist = root / "com.user.etsy-backup.weekly.plist"

    return {
        "plists": {
            "daily": {"exists": daily_plist.exists(), "path": str(daily_plist)},
            "weekly": {"exists": weekly_plist.exists(), "path": str(weekly_plist)},
        },
        "loaded": {
            "daily": loaded_daily,
            "weekly": loaded_weekly,
        },
        "configured": daily_plist.exists() and weekly_plist.exists(),
        "loaded_ok": loaded_daily and loaded_weekly,
        "status_evidence": _parse_backup_log(root / "output" / "backup" / "backup.log", root=root),
    }


def runtime_process_start_time_iso() -> str | None:
    """Return process start time in local ISO format."""
    pid = os.getpid()
    try:
        import psutil  # type: ignore

        create_time = psutil.Process(pid).create_time()
        return datetime.fromtimestamp(create_time, tz=None).astimezone().isoformat()
    except Exception:
        return None


def runtime_environment() -> dict[str, str]:
    return {
        "python_version": sys.version.split(" ")[0],
        "python_platform": sys.platform,
        "platform": platform.platform(),
        "python_implementation": platform.python_implementation(),
    }


def runtime_health_payload(
    *,
    runtime_root: Path,
    listen_host: str,
    listen_port: int,
    service_readiness: Mapping[str, Any],
    core_service_readiness: Mapping[str, Any] | bool | None = None,
    optional_service_readiness: Mapping[str, Any] | bool | None = None,
    active_shop_id: str,
    active_shop_name: str,
) -> dict[str, Any]:
    source = source_identity(runtime_root)
    backup = backup_scheduler_identity(runtime_root)
    now = datetime.now().astimezone().isoformat()
    source_match = is_canonical_runtime(runtime_root)

    file_hashes = source.get("file_hashes", {})
    source_commit = source.get("commit")
    source_dirty = source.get("dirty")
    service_checks = dict(service_readiness)
    service_boolean_checks = {
        key: bool(value)
        for key, value in service_checks.items()
        if isinstance(value, bool)
    }
    service_ok = all(service_boolean_checks.values()) if service_boolean_checks else False
    optional_checks = {
        key: bool(value)
        for key, value in service_checks.items()
        if isinstance(value, bool) and key in {"vertex_app", "mlx_ai", "watcher"}
    }
    optional_ok = all(optional_checks.values()) if optional_checks else False

    if isinstance(core_service_readiness, Mapping):
        core_readiness_ok = bool(core_service_readiness.get("ok")) if isinstance(
            core_service_readiness.get("ok"), bool
        ) else None
        core_readiness_checks = dict(core_service_readiness.get("checks") or {})
        if core_readiness_ok is None:
            core_readiness_ok = (
                dict(core_readiness_checks).get("dashboard_endpoint") is True
            )
            if not isinstance(core_readiness_ok, bool):
                core_readiness_ok = service_ok
    elif isinstance(core_service_readiness, bool):
        core_readiness_ok = core_service_readiness
        core_readiness_checks = {}
    else:
        core_readiness_ok = service_ok
        core_readiness_checks = {"dashboard_endpoint": True}

    if isinstance(optional_service_readiness, Mapping):
        optional_readiness_ok = (
            bool(optional_service_readiness.get("ok"))
            if isinstance(optional_service_readiness.get("ok"), bool)
            else None
        )
        optional_readiness_checks = dict(optional_service_readiness.get("checks") or {})
        if optional_readiness_ok is None:
            fallback_optional_checks = dict(
                optional_readiness_checks
                if optional_readiness_checks
                else optional_checks
            )
            optional_readiness_ok = (
                all(fallback_optional_checks.values()) if fallback_optional_checks else optional_ok
            )
    elif isinstance(optional_service_readiness, bool):
        optional_readiness_ok = optional_service_readiness
        optional_readiness_checks = optional_checks
    else:
        optional_readiness_ok = optional_ok
        optional_readiness_checks = optional_checks
    evidence = backup.get("status_evidence")
    last_success = evidence.get("last_success") if evidence else None
    last_failure = evidence.get("last_failure") if evidence else None
    backup_failed = _backup_failure_is_current(
        last_success=last_success if isinstance(last_success, dict) else None,
        last_failure=last_failure if isinstance(last_failure, dict) else None,
    )

    return {
        "generated_at": now,
        "canonical_root": str(canonical_root()),
        "current_root": str(runtime_root),
        "canonical_match": source_match,
        "source": {
            "commit": source_commit,
            "dirty": source_dirty,
            "file_hashes": file_hashes,
        },
        "frontend_assets": {
            "index_hash": file_hashes.get("dashboard_static/index.html"),
            "style_hash": file_hashes.get("dashboard_static/style.css"),
            "app_hash": file_hashes.get("dashboard_static/app.js"),
            "identity_stale": bool(source_dirty) or source_commit is None,
        },
        "process": {
            "pid": os.getpid(),
            "start_time_iso": runtime_process_start_time_iso(),
            "listen": {"host": listen_host, "port": listen_port},
        },
        "active_shop": {"id": active_shop_id, "name": active_shop_name},
        "backup_scheduler": {
            "loaded": backup.get("loaded", {}),
            "plists": backup.get("plists", {}),
            "configured": backup.get("configured", False),
            "loaded_ok": backup.get("loaded_ok", False),
            "status_evidence": {
                "last_success": last_success,
                "last_failure": last_failure,
            },
        },
        "python": runtime_environment(),
        "services": service_checks,
        "service_readiness": {
            "ok": core_readiness_ok,
            "checks": service_checks,
            "core": {
                "ok": core_readiness_ok,
                "checks": core_readiness_checks,
            },
            "optional": {
                "ok": optional_readiness_ok,
                "checks": optional_readiness_checks,
            },
        },
        "health_summary": {
            "source_stale": bool(source_dirty) or source_commit is None,
            "backup_scheduler_unloaded": not backup.get("loaded_ok", False),
            "backup_last_failure": backup_failed,
        },
    }
