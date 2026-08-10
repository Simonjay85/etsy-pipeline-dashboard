"""Per-shop Chrome session configuration for social publishing."""

from __future__ import annotations

import json
import hashlib
import re
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


SOCIAL_URLS = {
    "instagram": "https://www.instagram.com/",
    "pinterest": "https://www.pinterest.com/",
    "facebook": "https://www.facebook.com/",
    "twitter": "https://x.com/",
    "medium": "https://medium.com/",
    "reddit": "https://www.reddit.com/",
}
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
DEFAULT_PORT_START = 20000
DEFAULT_PORT_SPAN = 20000


@dataclass(frozen=True)
class SocialBrowserSession:
    shop_id: str
    profile_dir: Path
    debug_port: int

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.debug_port}"


def _safe_shop_name(shop_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", shop_id).strip(".-")
    return safe or "shop"


def resolve_social_session(
    base_dir: Path, shops: dict, shop_id: str
) -> SocialBrowserSession:
    """Resolve a dedicated profile/port, falling back safely on bad config.

    Supported config:
      "social_browser": {"profile_dir": "...", "debug_port": 19320}

    Explicit duplicate ports or profile directories are ignored for the later
    shop, so one shop can never silently attach to another shop's session.
    """
    if shop_id not in shops:
        raise KeyError(f"Unknown shop: {shop_id}")

    ordered_ids = sorted(str(key) for key in shops)
    explicit_ports: dict[str, int] = {}
    reserved_explicit_ports: set[int] = set()
    for current_id in ordered_ids:
        raw = shops.get(current_id, {})
        browser_cfg = raw.get("social_browser", {}) if isinstance(raw, dict) else {}
        if not isinstance(browser_cfg, dict) or "debug_port" not in browser_cfg:
            continue
        try:
            explicit_port = int(browser_cfg["debug_port"])
        except (TypeError, ValueError):
            continue
        if (
            1024 <= explicit_port <= 65535
            and explicit_port not in reserved_explicit_ports
        ):
            explicit_ports[current_id] = explicit_port
            reserved_explicit_ports.add(explicit_port)

    used_ports: set[int] = set()
    reserved_profiles: set[Path] = set()
    resolved: dict[str, SocialBrowserSession] = {}

    for index, current_id in enumerate(ordered_ids):
        raw = shops.get(current_id, {})
        browser_cfg = raw.get("social_browser", {}) if isinstance(raw, dict) else {}
        if not isinstance(browser_cfg, dict):
            browser_cfg = {}

        default_profile = (
            Path.home()
            / f".etsy_social_browser_session_{_safe_shop_name(current_id)}"
        ).resolve()
        raw_profile = str(browser_cfg.get("profile_dir", "")).strip()
        if raw_profile:
            configured_profile = Path(raw_profile).expanduser()
            candidate_profile = (
                configured_profile
                if configured_profile.is_absolute()
                else base_dir / configured_profile
            ).resolve()
        else:
            candidate_profile = default_profile
        if candidate_profile in reserved_profiles:
            candidate_profile = default_profile
        if candidate_profile in reserved_profiles:
            candidate_profile = (
                Path.home()
                / f".etsy_social_browser_session_{_safe_shop_name(current_id)}_{index}"
            ).resolve()

        if current_id in explicit_ports:
            candidate_port = explicit_ports[current_id]
        else:
            digest = hashlib.sha256(current_id.encode("utf-8")).digest()
            candidate_port = DEFAULT_PORT_START + (
                int.from_bytes(digest[:4], "big") % DEFAULT_PORT_SPAN
            )
            attempts = 0
            while (
                candidate_port in used_ports
                or candidate_port in reserved_explicit_ports
            ):
                candidate_port += 1
                attempts += 1
                if candidate_port >= DEFAULT_PORT_START + DEFAULT_PORT_SPAN:
                    candidate_port = DEFAULT_PORT_START
                if attempts >= DEFAULT_PORT_SPAN:
                    raise ValueError("No unique Chrome debug port available")

        session = SocialBrowserSession(
            shop_id=current_id,
            profile_dir=candidate_profile,
            debug_port=candidate_port,
        )
        resolved[current_id] = session
        reserved_profiles.add(candidate_profile)
        used_ports.add(candidate_port)

    return resolved[shop_id]


def load_social_session(base_dir: Path, shop_id: str) -> SocialBrowserSession:
    config_path = base_dir / "shops_config.json"
    with config_path.open("r", encoding="utf-8") as handle:
        shops = json.load(handle)
    if not isinstance(shops, dict):
        raise ValueError("shops_config.json must contain an object")
    return resolve_social_session(base_dir, shops, shop_id)


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


def is_session_ready(session: SocialBrowserSession) -> bool:
    """Confirm CDP and the Chrome command both identify this shop profile."""
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


def open_cdp_tab(session: SocialBrowserSession, url: str) -> bool:
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


def open_social_browser(
    session: SocialBrowserSession, platform: str, wait_seconds: float = 3.0
) -> bool:
    """Open the exact shop profile. Return CDP readiness, not login status."""
    if platform not in SOCIAL_URLS:
        raise ValueError(f"Unsupported social platform: {platform}")
    if is_session_ready(session):
        return open_cdp_tab(session, SOCIAL_URLS[platform])
    if not CHROME_PATH.is_file():
        raise FileNotFoundError(f"Google Chrome not found: {CHROME_PATH}")

    session.profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(CHROME_PATH),
            f"--remote-debugging-port={session.debug_port}",
            f"--user-data-dir={session.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            SOCIAL_URLS[platform],
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
