"""Open the exact per-shop Etsy profile used by the listing poster."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
SHOP_MANAGER_URL = "https://www.etsy.com/your/shops/me/tools/listings"
DEFAULT_PORT_START = 41000
DEFAULT_PORT_SPAN = 8000
PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


class EtsyProfileLockedError(RuntimeError):
    """Raised when Chrome owns the posting profile outside our exact CDP session."""


@dataclass(frozen=True)
class EtsyBrowserSession:
    shop_id: str
    profile_dir: Path
    debug_port: int

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.debug_port}"


def _safe_shop_name(shop_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", shop_id).strip(".-")
    return safe or "shop"


def _configured_profile(base_dir: Path, shop_id: str, shop_config: dict) -> Path:
    raw_profile = str(shop_config.get("browser_session") or "").strip()
    if raw_profile:
        candidate = Path(raw_profile).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        return candidate.resolve()
    if shop_id == "templystudios":
        legacy = (base_dir / ".browser-session").resolve()
        if legacy.exists():
            return legacy
    return (Path.home() / f".etsy_browser_session_{_safe_shop_name(shop_id)}").resolve()


def resolve_etsy_session(
    base_dir: Path, shops: dict, shop_id: str
) -> EtsyBrowserSession:
    """Resolve the poster profile and a unique CDP port without sharing profiles."""
    if shop_id not in shops:
        raise KeyError(f"Shop không tồn tại: {shop_id}")

    ordered_ids = sorted(str(key) for key in shops)
    profiles: dict[str, Path] = {}
    used_profiles: dict[Path, str] = {}
    for current_id in ordered_ids:
        config = shops.get(current_id, {})
        if not isinstance(config, dict):
            config = {}
        profile = _configured_profile(base_dir, current_id, config)
        owner = used_profiles.get(profile)
        if owner is not None:
            raise ValueError(
                f"Profile Etsy bị dùng chung giữa shop {owner} và {current_id}: {profile}"
            )
        used_profiles[profile] = current_id
        profiles[current_id] = profile

    used_ports: set[int] = set()
    resolved_ports: dict[str, int] = {}
    for current_id in ordered_ids:
        config = shops.get(current_id, {})
        raw_port = config.get("etsy_login_debug_port") if isinstance(config, dict) else None
        try:
            configured_port = int(raw_port) if raw_port is not None else None
        except (TypeError, ValueError):
            configured_port = None
        if configured_port is not None and not 1024 <= configured_port <= 65535:
            configured_port = None

        if configured_port is None or configured_port in used_ports:
            digest = hashlib.sha256(current_id.encode("utf-8")).digest()
            candidate = DEFAULT_PORT_START + (
                int.from_bytes(digest[:4], "big") % DEFAULT_PORT_SPAN
            )
            while candidate in used_ports:
                candidate += 1
                if candidate >= DEFAULT_PORT_START + DEFAULT_PORT_SPAN:
                    candidate = DEFAULT_PORT_START
        else:
            candidate = configured_port
        used_ports.add(candidate)
        resolved_ports[current_id] = candidate

    return EtsyBrowserSession(
        shop_id=shop_id,
        profile_dir=profiles[shop_id],
        debug_port=resolved_ports[shop_id],
    )


def is_cdp_ready(debug_port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", debug_port), timeout=timeout):
            pass
        with urllib.request.urlopen(
            f"http://127.0.0.1:{debug_port}/json/version", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("webSocketDebuggerUrl"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def is_session_ready(session: EtsyBrowserSession) -> bool:
    """Require both live CDP and a Chrome command with this exact profile and port."""
    if not is_cdp_ready(session.debug_port):
        return False
    try:
        result = subprocess.run(
            ["ps", "-axo", "command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    port_arg = f"--remote-debugging-port={session.debug_port}"
    profile_arg = f"--user-data-dir={session.profile_dir}"
    return any(
        port_arg in line and profile_arg in line
        for line in result.stdout.splitlines()
        if "Google Chrome" in line
    )


def _profile_lock(session: EtsyBrowserSession) -> Path | None:
    return next(
        (
            session.profile_dir / lock_name
            for lock_name in PROFILE_LOCK_NAMES
            if (session.profile_dir / lock_name).exists()
        ),
        None,
    )


def open_cdp_tab(session: EtsyBrowserSession, url: str) -> bool:
    encoded_url = urllib.parse.quote(url, safe="")
    request = urllib.request.Request(
        f"{session.cdp_url}/json/new?{encoded_url}", method="PUT"
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("id"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def open_etsy_login_browser(
    session: EtsyBrowserSession, wait_seconds: float = 4.0
) -> bool:
    """Open Shop Manager in the exact poster profile, never copying cookies."""
    if is_session_ready(session):
        return open_cdp_tab(session, SHOP_MANAGER_URL)

    lock_path = _profile_lock(session)
    if lock_path is not None:
        raise EtsyProfileLockedError(
            f"Profile Etsy đang bị khóa ({lock_path.name}). "
            "Hãy đóng cửa sổ Chrome đang dùng profile Etsy này rồi thử lại."
        )
    if not CHROME_PATH.is_file():
        raise FileNotFoundError(f"Không tìm thấy Google Chrome: {CHROME_PATH}")

    session.profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(CHROME_PATH),
            f"--remote-debugging-port={session.debug_port}",
            f"--user-data-dir={session.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            SHOP_MANAGER_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if is_session_ready(session):
            return True
        time.sleep(0.15)
    return is_session_ready(session)
