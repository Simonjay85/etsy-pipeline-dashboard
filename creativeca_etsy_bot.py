#!/usr/bin/env python3
"""Creative Fabrica -> ChatGPT/DaisyFlow -> Etsy pipeline.

The bot is intentionally fail-closed:
- Creative Fabrica source and license evidence are kept under source_assets/.
- Only an explicit customer file is copied to files/.
- ChatGPT output is staged and must contain exactly ten distinct square images.
- Etsy posting reuses the existing verified poster and JobStore, creating a draft.
- Live publish requires --publish and --confirm-publish <shop-id>.

No credentials are read from command-line values or written to manifests. Browser
sessions must already be logged in by the user in the exact profile/CDP session.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parent
SHOP_CONFIG = ROOT / "shops_config.json"
POSTER = ROOT / "etsy_auto_post.py"
JOB_DB = ROOT / ".etsy-dashboard-diagnostics" / "etsy_jobs.sqlite"
RUN_ROOT = ROOT / "output" / "creativeca_bot"
DEFAULT_CHATGPT_CDP = os.environ.get("DAISYFLOW_CHATGPT_CDP_URL", "http://127.0.0.1:9333")
DEFAULT_GPT_URL = os.environ.get(
    "DAISYFLOW_GPT_URL",
    "https://chatgpt.com/images/",
)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
CUSTOMER_EXTS = {".pdf", ".zip"}
SOURCE_HOST = "www.creativefabrica.com"
WORKER_ID = "creativeca-etsy-bot"
# The DaisyFlow composer accepts small ZIPs but keeps a 31 MB source ZIP in
# aria-disabled state. Keep the canonical source locally and upload a bounded
# evidence bundle containing its manifest plus verified page previews.
CHATGPT_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
CHATGPT_MAX_TOTAL_BYTES = 24 * 1024 * 1024

try:
    import openpyxl
except ImportError:  # pragma: no cover - runtime diagnostic is clearer in main
    openpyxl = None  # type: ignore[assignment]

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    UnidentifiedImageError = OSError  # type: ignore[assignment,misc]

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment,misc]
    sync_playwright = None  # type: ignore[assignment]

from etsy_browser_session import is_session_ready, resolve_etsy_session
from job_store import JobStore
from pipeline_artifacts import read_pipeline_product_manifest, write_pipeline_product_manifest
from pipeline_contracts import PipelineContractError, source_fingerprint, validate_final_artifact


class BotError(RuntimeError):
    """A safe, actionable pipeline failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_chatgpt_source(
    source_zip: Path,
    stage_dir: Path,
    reference_paths: list[Path],
) -> Path:
    """Return a ChatGPT-safe evidence archive without replacing canonical source.

    ChatGPT's DaisyFlow composer can leave large ZIP uploads disabled. The
    original Creative Fabrica ZIP remains authoritative under source_assets/;
    when it exceeds the observed safe limit, upload a small evidence archive
    containing the original hash/member manifest and verified page previews.
    """
    source_zip = _require_file(source_zip, "Creative Fabrica source ZIP")
    if source_zip.stat().st_size <= CHATGPT_MAX_ATTACHMENT_BYTES:
        return source_zip

    source_assets = stage_dir / "source_assets"
    source_assets.mkdir(parents=True, exist_ok=True)
    evidence_zip = source_assets / "chatgpt-source-evidence.zip"
    manifest: dict[str, Any] = {
        "schema": "creativeca-chatgpt-source-evidence.v1",
        "created_at": utc_now(),
        "canonical_source_zip": source_zip.name,
        "canonical_source_sha256": sha256_file(source_zip),
        "canonical_source_bytes": source_zip.stat().st_size,
        "reference_files": [],
        "note": "This archive is an upload-safe evidence subset; the canonical ZIP stays local and authoritative.",
    }
    try:
        with zipfile.ZipFile(source_zip) as archive:
            members = []
            for info in archive.infolist():
                if info.is_dir():
                    continue
                members.append({"name": info.filename, "bytes": info.file_size})
            manifest["canonical_members"] = members
    except (OSError, zipfile.BadZipFile) as exc:
        raise BotError(f"Không đọc được source ZIP để tạo evidence bundle: {source_zip}") from exc

    # Page references are already verified downloads and are the most compact,
    # faithful visual evidence. If none exist, include up to twelve raster
    # previews directly from the canonical archive.
    entries: list[tuple[str, bytes]] = []
    for path in reference_paths[:12]:
        path = _require_file(path, "Creative Fabrica reference")
        if path.suffix.lower() not in IMAGE_EXTS:
            continue
        entries.append((f"references/{safe_name(path.name, 'reference.jpg')}", path.read_bytes()))
    if not entries:
        try:
            with zipfile.ZipFile(source_zip) as archive:
                for info in archive.infolist():
                    if info.is_dir() or Path(info.filename).suffix.lower() not in IMAGE_EXTS:
                        continue
                    entries.append((f"archive_previews/{safe_name(Path(info.filename).name, 'preview.png')}", archive.read(info)))
                    if len(entries) >= 12:
                        break
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            raise BotError("Không tạo được preview từ source ZIP lớn") from exc
    if not entries:
        raise BotError("Source ZIP lớn không có reference/preview raster để upload ChatGPT")

    # Large raster previews can still exceed the composer limit. Try the full
    # evidence set first, then reduce deterministically while retaining the
    # canonical ZIP manifest and at least one faithful preview.
    tmp = evidence_zip.with_suffix(".zip.tmp")
    selected_entries: list[tuple[str, bytes]] = []
    try:
        for keep in (len(entries), 8, 6, 4, 3, 2, 1):
            selected_entries = entries[:keep]
            manifest["reference_files"] = [
                {"archive_name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in selected_entries
            ]
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr("SOURCE_EVIDENCE.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                for name, data in selected_entries:
                    archive.writestr(name, data)
            tmp.replace(evidence_zip)
            if evidence_zip.stat().st_size <= CHATGPT_MAX_ATTACHMENT_BYTES:
                atomic_json(stage_dir / "chatgpt-source-evidence.json", manifest)
                return evidence_zip
    finally:
        if tmp.exists():
            tmp.unlink()
    raise BotError(
        f"Evidence bundle ChatGPT vẫn quá lớn: {evidence_zip.stat().st_size} bytes; "
        "cần giảm số preview trước khi upload"
    )


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def safe_name(value: str, fallback: str = "file") -> str:
    name = Path(str(value or "")).name
    name = re.sub(r"[^A-Za-z0-9._() -]+", "_", name).strip(" ._")
    return name or fallback


def canonical_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    return f"https://{parsed.netloc}{parsed.path.rstrip('/')}"


def validate_creative_fabrica_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    host = parsed.netloc.lower().split(":", 1)[0]
    if parsed.scheme != "https" or host not in {SOURCE_HOST, "creativefabrica.com"}:
        raise BotError("Creative Fabrica source URL phải là HTTPS trên creativefabrica.com")
    if not parsed.path.startswith(("/product/", "/subscriptions/graphics/")):
        raise BotError("Source URL không phải trang sản phẩm/danh mục Creative Fabrica được phép")
    return raw


def _require_file(path: Path, label: str) -> Path:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise BotError(f"{label} không tồn tại hoặc là symlink: {path}")
    if path.stat().st_size <= 0:
        raise BotError(f"{label} rỗng: {path}")
    return path.resolve()


def load_json(path: Path, label: str) -> dict[str, Any]:
    path = _require_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BotError(f"{label} không phải JSON hợp lệ: {path}") from exc
    if not isinstance(value, dict):
        raise BotError(f"{label} phải là object JSON: {path}")
    return value


def normalize_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[,;\n]+", str(value or ""))
    tags: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = str(item or "").strip()
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        if len(tag) > 20:
            raise BotError(f"Tag Etsy vượt 20 ký tự: {tag}")
        seen.add(key)
        tags.append(tag)
    return tags


def validate_listing_payload(payload: dict[str, Any], source_url: str) -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    tags = normalize_tags(payload.get("tags"))
    if not title or len(title) > 140:
        raise BotError("listing.json cần title dài 1-140 ký tự")
    if not description or len(description) < 20:
        raise BotError("listing.json cần description tối thiểu 20 ký tự")
    if len(tags) != 13:
        raise BotError(f"listing.json cần đúng 13 tag duy nhất, hiện có {len(tags)}")
    if not re.search(r"https?://", description):
        raise BotError("description phải có URL evidence/licensing để không tạo claim không nguồn")
    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError) as exc:
        raise BotError("listing.json price phải là số dương") from exc
    if price <= 0 or price > 10000:
        raise BotError("listing.json price phải trong khoảng >0 và <=10000")
    result = dict(payload)
    result.update(
        {
            "title": title,
            "description": description,
            "tags": tags,
            "price": price,
            "qty": int(payload.get("qty") or 999),
            "category": str(payload.get("category") or "Digital").strip(),
            "section": str(payload.get("section") or "Digital Planner").strip(),
            "when_made": str(payload.get("when_made") or "2020_2026").strip(),
            "keywords": str(payload.get("keywords") or "").strip(),
            "source_url": source_url,
        }
    )
    if result["qty"] < 1:
        raise BotError("listing.json qty phải >= 1")
    return result


def load_license(path: Path, source_url: str) -> dict[str, Any]:
    payload = load_json(path, "license.json")
    expected = canonical_url(source_url)
    declared = canonical_url(str(payload.get("source_url") or ""))
    if declared != expected:
        raise BotError("license.json source_url không khớp chính xác source URL")
    if payload.get("resale_allowed") is not True:
        raise BotError("Bot dừng: license.json chưa xác nhận resale_allowed=true")
    license_name = str(payload.get("license") or payload.get("license_name") or "").strip()
    if not license_name:
        raise BotError("license.json thiếu tên license")
    payload.update(
        {
            "source_url": source_url,
            "license": license_name,
            "resale_allowed": True,
            "checked_at": str(payload.get("checked_at") or utc_now()),
        }
    )
    return payload


def load_prompt(path: Path) -> str:
    path = _require_file(path, "prompt file")
    prompt = path.read_text(encoding="utf-8")
    if not prompt.strip() or "\x00" in prompt:
        raise BotError("prompt file rỗng hoặc chứa ký tự điều khiển")
    return prompt


def read_shop_config(shop_id: str) -> dict[str, Any]:
    shops = load_json(SHOP_CONFIG, "shops_config.json")
    cfg = shops.get(shop_id)
    if not isinstance(cfg, dict):
        raise BotError(f"Shop không tồn tại trong shops_config.json: {shop_id}")
    return {"all": shops, **cfg}


def checkpoint(run_dir: Path, stage: str, **extra: Any) -> None:
    payload = {"schema": "creativeca-etsy-bot.v1", "stage": stage, "updated_at": utc_now(), **extra}
    atomic_json(run_dir / "checkpoint.json", payload)


def require_runtime() -> None:
    missing = []
    if openpyxl is None:
        missing.append("openpyxl")
    if Image is None:
        missing.append("Pillow")
    if sync_playwright is None:
        missing.append("playwright")
    if missing:
        raise BotError("Thiếu runtime: " + ", ".join(missing))


@contextmanager
def connected_browser(cdp_url: str):
    if sync_playwright is None:
        raise BotError("Thiếu Playwright; không thể kết nối browser")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=30000)
            if not browser.contexts:
                raise BotError(f"CDP không có browser context: {cdp_url}")
            yield browser, browser.contexts[0]
    except BotError:
        raise
    except Exception as exc:
        raise BotError(f"Không kết nối được browser CDP {cdp_url}: {exc}") from exc


def pick_page(context, contains: str | None = None):
    pages = list(context.pages)
    if contains:
        for page in pages:
            if contains.lower() in str(page.url).lower():
                return page
    if pages:
        return pages[0]
    return context.new_page()


def page_body(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=10000)
    except Exception:
        return ""


def creative_fabrica_download(
    source_url: str,
    stage_dir: Path,
    cdp_url: str,
) -> dict[str, Any]:
    source_url = validate_creative_fabrica_url(source_url)
    stage_dir.mkdir(parents=True, exist_ok=True)
    source_assets = stage_dir / "source_assets"
    refs_dir = source_assets / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    zip_path = source_assets / "creative-fabrica-source.zip"
    image_paths: list[Path] = []
    with connected_browser(cdp_url) as (_browser, context):
        page = pick_page(context, "creativefabrica.com")
        try:
            response = page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            raise BotError(f"Creative Fabrica không mở được source page: {exc}") from exc
        status = getattr(response, "status", None)
        body = page_body(page)
        title = str(page.title() or "")
        if status in {401, 403, 429} or "/login" in str(page.url).lower() or "sign in" in body.lower():
            raise BotError("Creative Fabrica cần đăng nhập/challenge; hãy đăng nhập trong đúng browser session rồi chạy lại")
        if status is not None and int(status) >= 400:
            raise BotError(f"Creative Fabrica trả HTTP {status}")
        if any(marker in title.lower() for marker in ("just a moment", "access denied", "verify you are human")):
            raise BotError("Creative Fabrica đang yêu cầu browser challenge; bot dừng")
        (source_assets / "source-page.html").write_text(page.content(), encoding="utf-8")
        (source_assets / "source-page.txt").write_text(body[:200000], encoding="utf-8")

        download_selectors = [
            'a[download]',
            'a:has-text("Download")',
            'button:has-text("Download")',
            '[role="button"]:has-text("Download")',
        ]
        download_error = ""
        for selector in download_selectors:
            loc = page.locator(selector)
            for index in range(min(await_count(loc), 12)):
                item = loc.nth(index)
                try:
                    if not item.is_visible():
                        continue
                    with page.expect_download(timeout=8000) as info:
                        item.click(force=True)
                    download = info.value
                    download.save_as(str(zip_path))
                    if zip_path.stat().st_size > 0:
                        break
                except Exception as exc:  # try the next UI candidate
                    download_error = str(exc)
            if zip_path.is_file() and zip_path.stat().st_size > 0:
                break

        if not zip_path.is_file() or zip_path.stat().st_size <= 0:
            hrefs = page.locator("a[href]").evaluate_all(
                """els => els.map(a => ({href: a.href || a.getAttribute('href') || '', text: a.innerText || ''}))
                .filter(x => /download/i.test(x.text + ' ' + x.href)).slice(0, 20)"""
            )
            for item in hrefs or []:
                href = urljoin(page.url, str(item.get("href") or ""))
                parsed = urlsplit(href)
                host = parsed.netloc.lower().split(":", 1)[0]
                if parsed.scheme != "https" or not (host == SOURCE_HOST or host.endswith(".creativefabrica.com")):
                    continue
                try:
                    api = context.request.get(href, timeout=60000)
                    body_bytes = api.body()
                    if api.status == 200 and body_bytes[:2] == b"PK":
                        zip_path.write_bytes(body_bytes)
                        break
                except Exception as exc:
                    download_error = str(exc)

        if not zip_path.is_file() or zip_path.stat().st_size <= 0:
            raise BotError(
                "Không tải được ZIP Creative Fabrica; cần đăng nhập/quyền download hoặc selector đã đổi"
                + (f": {download_error[:180]}" if download_error else "")
            )

        # Prefer the OpenGraph image and product-gallery images. Keep the source
        # ZIP authoritative; these files are references only.
        image_urls: list[str] = []
        og = page.locator('meta[property="og:image"]').first
        if await_count(og):
            value = og.get_attribute("content")
            if value:
                image_urls.append(urljoin(page.url, value))
        image_urls.extend(
            page.locator("img").evaluate_all(
                """els => els.map(img => img.currentSrc || img.src || img.getAttribute('data-src') || '')
                .filter(Boolean).filter(x => x.startsWith('https://'))"""
            )
        )
        seen: set[str] = set()
        for raw in image_urls:
            url = str(raw).split("#", 1)[0]
            if url in seen or len(image_paths) >= 12:
                continue
            seen.add(url)
            try:
                api = context.request.get(url, timeout=30000)
                content_type = str(api.headers.get("content-type") or "").lower()
                data = api.body()
                if api.status != 200 or not content_type.startswith("image/") or len(data) < 1000:
                    continue
                ext = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
                    content_type.split(";", 1)[0], ".jpg"
                )
                path = refs_dir / f"reference_{len(image_paths) + 1:02d}{ext}"
                path.write_bytes(data)
                image_paths.append(path)
            except Exception:
                continue

    try:
        with zipfile.ZipFile(zip_path) as archive:
            if not archive.namelist():
                raise BotError("ZIP Creative Fabrica không có member")
    except (OSError, zipfile.BadZipFile) as exc:
        raise BotError(f"ZIP Creative Fabrica tải về không hợp lệ: {zip_path}") from exc

    receipt = {
        "schema": "creativeca-download.v1",
        "source_url": source_url,
        "zip": str(zip_path.relative_to(stage_dir)),
        "zip_sha256": sha256_file(zip_path),
        "references": [str(path.relative_to(stage_dir)) for path in image_paths],
        "downloaded_at": utc_now(),
    }
    atomic_json(stage_dir / "download.json", receipt)
    return {"zip": zip_path, "references": image_paths, "receipt": receipt}


def await_count(locator) -> int:
    try:
        return int(locator.count())
    except Exception:
        return 0


def estuary_assets(page) -> dict[str, str]:
    urls = page.evaluate(
        """() => [...document.querySelectorAll('img')]
        .map(img => img.currentSrc || img.src || '')
        .filter(src => src.includes('/backend-api/estuary/content'))"""
    )
    result: dict[str, str] = {}
    for raw in urls or []:
        value = str(raw or "")
        match = re.search(r"[?&]id=([^&]+)", value)
        if match:
            result.setdefault(match.group(1), value)
    return result


def normalize_editor_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("\ufeff", "")


def prompt_text_without_tool_pill(editor) -> str:
    """Read only editable composer text, excluding the Create image pill."""
    try:
        value = editor.evaluate(
            """e => {
                const clone = e.cloneNode(true);
                clone.querySelectorAll('[contenteditable="false"], [data-inline-selection-pill]')
                    .forEach(node => node.remove());
                return clone.innerText || clone.textContent || '';
            }"""
        )
        return str(value or "")
    except Exception:
        return str(editor.inner_text() or "")


def strip_create_image_prefix(value: str) -> str:
    return re.sub(r"^\s*@?Create image\s*", "", str(value or ""), count=1, flags=re.IGNORECASE)


def _canonical_upload_name(value: str) -> str:
    path = Path(str(value or "").strip())
    suffix = path.suffix
    stem = re.sub(r"\(\d+\)$", "", path.stem)
    return f"{stem}{suffix}".lower()


def chatgpt_attachment_labels(page) -> list[str]:
    locator = page.locator('[aria-label^="Remove file" i]')
    labels: list[str] = []
    for index in range(await_count(locator)):
        value = locator.nth(index).get_attribute("aria-label") or ""
        if ":" in value:
            labels.append(value.split(":", 1)[1].strip())
    return labels


def is_chat_images_route(url: str) -> bool:
    return urlsplit(str(url or "")).path.rstrip("/").endswith("/images")


def reset_chatgpt_composer(page, gpt_url: str):
    """Open the Chat/Images route and return a clean hydrated editor."""
    base_url = re.sub(r"/(?:project|c/[^/]+)(?:/)?$", "", str(gpt_url).rstrip("/"))
    if "/project" in base_url:
        raise BotError("DaisyFlow gpt-url đang trỏ Work/project; dùng https://chatgpt.com/images/ cho Chat")
    try:
        page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1200)
        page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        raise BotError(f"Không mở được DaisyFlow ChatGPT worker: {exc}") from exc
    deadline = time.monotonic() + 45
    editor = None
    while time.monotonic() < deadline:
        candidate = page.locator("#prompt-textarea").first
        if not await_count(candidate):
            candidate = page.locator('div[contenteditable="true"]').last
        if await_count(candidate) and candidate.is_visible() and await_count(page.locator('input[type="file"]')):
            editor = candidate
            break
        page.wait_for_timeout(1000)
    if editor is None:
        raise BotError("ChatGPT composer không hydrate trong 45 giây")
    if "/auth/login" in str(page.url).lower() or "log in" in page_body(page).lower()[:2000]:
        raise BotError("ChatGPT worker chưa đăng nhập; hãy hoàn tất login trong đúng Chrome UI")
    if "/project" in str(page.url).lower():
        raise BotError("ChatGPT worker đang ở Work/project; bot chỉ được chạy Chat/Images")
    # The custom GPT route may preload a default prompt and duplicate tool pills.
    # Filling the editor is the supported React path that clears those stale pills.
    editor.click()
    editor.fill("")
    page.wait_for_timeout(500)
    labels = chatgpt_attachment_labels(page)
    if labels:
        raise BotError(f"ChatGPT composer chưa sạch: còn attachments {labels}")
    if not is_chat_images_route(page.url) and estuary_assets(page):
        raise BotError("ChatGPT conversation chưa sạch: còn generated IDs; không gửi trùng")
    return editor


def wait_for_chatgpt_attachments(page, attachments: list[Path], timeout_seconds: int = 120) -> None:
    expected = sorted(_canonical_upload_name(path.name) for path in attachments)
    deadline = time.monotonic() + timeout_seconds
    last_labels: list[str] = []
    while time.monotonic() < deadline:
        last_labels = chatgpt_attachment_labels(page)
        actual = sorted(_canonical_upload_name(value) for value in last_labels)
        if actual == expected:
            return
        page.wait_for_timeout(1000)
    raise BotError(
        "ChatGPT attachment chưa sẵn sàng đúng danh sách sau "
        f"{timeout_seconds}s; expected={expected}, actual={last_labels}"
    )


def wait_for_chatgpt_send(page, timeout_seconds: int = 90) -> None:
    send = page.locator('button[data-testid="send-button"], button[aria-label="Send message"]').last
    if not await_count(send) or not send.is_visible():
        raise BotError("ChatGPT send button chưa sẵn sàng")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if send.is_visible() and send.is_enabled() and send.get_attribute("aria-disabled") != "true":
            return
        page.wait_for_timeout(1000)
    raise BotError(
        "ChatGPT Send vẫn disabled sau upload; kiểm tra giới hạn file/session "
        f"(attachments={chatgpt_attachment_labels(page)})"
    )


def activate_create_image(page) -> None:
    """Select the current UI's Create image composer mode exactly once."""
    active = page.locator('[data-inline-selection-pill][data-keyword="Create image" i]')
    active_count = await_count(active)
    if active_count == 1:
        return
    if active_count > 1:
        raise BotError(f"Composer có {active_count} Create image pills; dừng để tránh gửi sai mode")
    plus = page.locator('button[aria-label="Add files and more"], button[data-testid="composer-plus-btn"]').last
    if not await_count(plus) or not plus.is_visible():
        raise BotError("Không tìm thấy nút mở composer tools để chọn Create image")
    plus.click()
    item = None
    for _ in range(20):
        span = page.get_by_text("Create image", exact=True).first
        candidate = span.locator('xpath=ancestor::div[contains(@class,"__menu-item")]').first
        if await_count(candidate) and candidate.is_visible():
            item = candidate
            break
        page.wait_for_timeout(500)
    if item is None:
        raise BotError("Không tìm thấy menu item Create image")
    item.click()
    page.wait_for_timeout(700)
    if not await_count(page.locator('[data-inline-selection-pill][data-keyword="Create image" i]')):
        raise BotError("Create image chưa được kích hoạt trong composer")

def generate_chatgpt_images(
    stage_dir: Path,
    source_zip: Path,
    reference_paths: list[Path],
    prompt: str,
    cdp_url: str,
    gpt_url: str,
    timeout_seconds: int = 900,
) -> list[Path]:
    images_dir = stage_dir / "generated_images"
    images_dir.mkdir(parents=True, exist_ok=True)
    attachments = [source_zip, *reference_paths]
    attachment_bytes = 0
    for path in attachments:
        path = _require_file(path, "ChatGPT attachment")
        size = path.stat().st_size
        if size > CHATGPT_MAX_ATTACHMENT_BYTES:
            raise BotError(
                f"ChatGPT attachment quá lớn ({size} bytes): {path.name}; "
                "hãy dùng prepare_chatgpt_source()"
            )
        attachment_bytes += size
    if attachment_bytes > CHATGPT_MAX_TOTAL_BYTES:
        raise BotError(
            f"Tổng ChatGPT attachments quá lớn ({attachment_bytes} bytes); "
            "hãy giảm reference/evidence trước khi upload"
        )
    with connected_browser(cdp_url) as (_browser, context):
        page = pick_page(context, "chatgpt.com")
        editor = reset_chatgpt_composer(page, gpt_url)
        # /images/ renders Library thumbnails before send; those IDs are not a
        # baseline for the new request. The post-send conversation is scoped
        # separately below.
        baseline = {} if is_chat_images_route(page.url) else estuary_assets(page)
        file_input = page.locator("#upload-files").first
        if not await_count(file_input):
            file_input = page.locator('input[type="file"]').first
        if not await_count(file_input):
            raise BotError("Không tìm thấy ChatGPT file input")
        try:
            file_input.set_input_files([str(path) for path in attachments])
        except Exception as exc:
            raise BotError(f"Upload attachments vào ChatGPT thất bại: {exc}") from exc
        wait_for_chatgpt_attachments(page, attachments)

        # The Images route is already the Chat Images generator; forcing an
        # inline tool pill there is unsupported. Custom non-Images routes must
        # still activate exactly one Create image pill.
        editor.click()
        editor.fill(prompt)
        if not is_chat_images_route(page.url):
            activate_create_image(page)
        entered = prompt_text_without_tool_pill(editor)
        if normalize_editor_text(entered) != normalize_editor_text(prompt):
            raise BotError("Prompt trong composer không khớp prompt file; dừng trước khi gửi")
        wait_for_chatgpt_send(page)
        send = page.locator('button[data-testid="send-button"], button[aria-label="Send message"]').last
        send.click()
        if is_chat_images_route(gpt_url):
            route_deadline = time.monotonic() + 90
            while time.monotonic() < route_deadline:
                path = urlsplit(str(page.url)).path
                if re.search(r"/c/(?!WEB:)[^/]+$", path):
                    break
                page.wait_for_timeout(1000)
            if not re.search(r"/c/(?!WEB:)[^/]+$", urlsplit(str(page.url)).path):
                raise BotError("Chat Images 2.5 không chuyển sang conversation thật sau Send")
        page.wait_for_timeout(1500)
        user_messages = page.locator('[data-message-author-role="user"]')
        submitted = ""
        verify_deadline = time.monotonic() + 30
        while time.monotonic() < verify_deadline:
            if await_count(user_messages):
                submitted = normalize_editor_text(
                    strip_create_image_prefix(user_messages.last.inner_text())
                )
                if submitted.endswith(normalize_editor_text(prompt)):
                    break
            page.wait_for_timeout(1000)
        expected_prompt = normalize_editor_text(prompt)
        # ChatGPT renders attachment metadata before the text bubble; verify
        # the exact prompt as the final normalized segment, never compare the
        # whole user-message container against prompt text.
        if not submitted.endswith(expected_prompt):
            raise BotError("Không xác minh được prompt đã gửi nguyên văn")

        captured: dict[str, str] = {}
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for asset_id, url in estuary_assets(page).items():
                if asset_id not in baseline:
                    captured.setdefault(asset_id, url)
            if len(captured) >= 10:
                break
            page.wait_for_timeout(5000)
        if len(captured) < 10:
            raise BotError(f"ChatGPT chưa tạo đủ 10 ảnh riêng biệt; mới có {len(captured)}")

        outputs: list[Path] = []
        for index, url in enumerate(list(captured.values())[:10], 1):
            try:
                response = context.request.get(url, timeout=120000)
                data = response.body()
                if response.status != 200 or len(data) < 5000:
                    raise BotError(f"Ảnh ChatGPT {index} tải thất bại HTTP={response.status} bytes={len(data)}")
                if Image is None:
                    raise BotError("Thiếu Pillow để kiểm tra ảnh ChatGPT")
                with Image.open(BytesIO(data)) as image:
                    image.verify()
                output = images_dir / f"etsy_{index:02d}.png"
                with Image.open(BytesIO(data)) as image:
                    image.convert("RGB").save(output, "PNG")
                outputs.append(output)
            except BotError:
                raise
            except Exception as exc:
                raise BotError(f"Ảnh ChatGPT {index} không đọc được: {exc}") from exc

    validate_image_set(outputs)
    atomic_json(
        stage_dir / "generation.json",
        {
            "schema": "creativeca-chatgpt-generation.v1",
            "generated_at": utc_now(),
            "count": len(outputs),
            "files": [str(path.relative_to(stage_dir)) for path in outputs],
            "sha256": [sha256_file(path) for path in outputs],
            "source_attachment": str(source_zip.relative_to(stage_dir)),
            "source_attachment_bytes": source_zip.stat().st_size,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        },
    )
    return outputs


def validate_image_set(paths: Iterable[Path]) -> list[dict[str, Any]]:
    items = list(paths)
    if len(items) != 10:
        raise BotError(f"Cần đúng 10 ảnh listing, nhận {len(items)}")
    seen: set[str] = set()
    evidence: list[dict[str, Any]] = []
    for path in items:
        path = _require_file(path, "listing image")
        if path.suffix.lower() not in IMAGE_EXTS:
            raise BotError(f"Listing image extension không hợp lệ: {path}")
        digest = sha256_file(path)
        if digest in seen:
            raise BotError(f"Listing image bị trùng hash: {path.name}")
        seen.add(digest)
        if Image is None:
            raise BotError("Thiếu Pillow để validate listing image")
        try:
            with Image.open(path) as image:
                width, height = image.size
                image.verify()
            if width != height or width < 256:
                raise BotError(f"Listing image phải vuông tối thiểu 256px: {path.name}={width}x{height}")
        except BotError:
            raise
        except (OSError, UnidentifiedImageError) as exc:
            raise BotError(f"Listing image không decode được: {path}") from exc
        evidence.append({"path": str(path), "sha256": digest, "size": path.stat().st_size})
    return evidence


def allocate_product(shop_root: Path, workbook: Path) -> tuple[str, int]:
    names: set[str] = set()
    for path in shop_root.iterdir() if shop_root.exists() else []:
        if path.is_dir() and not path.is_symlink() and re.fullmatch(r"product-\d+", path.name):
            names.add(path.name)
    if openpyxl is None:
        raise BotError("Thiếu openpyxl để allocate product")
    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    try:
        ws = wb["Listings"]
        for row in ws.iter_rows(min_row=4, values_only=True):
            value = str(row[1] or "").strip() if len(row) > 1 else ""
            if re.fullmatch(r"product-\d+", value):
                names.add(value)
    finally:
        wb.close()
    numbers = [int(name.split("-", 1)[1]) for name in names]
    number = max(numbers, default=0) + 1
    while (shop_root / f"product-{number}").exists():
        number += 1
    return f"product-{number}", number


def materialize_product(
    shop_root: Path,
    stage_dir: Path,
    source_zip: Path,
    references: list[Path],
    generated_images: list[Path],
    customer_file: Path,
    license_payload: dict[str, Any],
    listing_payload: dict[str, Any],
) -> tuple[Path, str, int]:
    shop_root = shop_root.resolve()
    workbook = shop_root / "Etsy_SEO_Generator.xlsx"
    if not workbook.is_file():
        raise BotError(f"Thiếu workbook canonical: {workbook}")
    folder, number = allocate_product(shop_root, workbook)
    product_root = shop_root / folder
    product_root.mkdir(parents=False, exist_ok=False)
    try:
        for subdir in ("source_assets/references", "images", "files"):
            (product_root / subdir).mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_zip, product_root / "source_assets" / safe_name(source_zip.name, "source.zip"))
        for path in references:
            shutil.copy2(path, product_root / "source_assets" / "references" / safe_name(path.name, "reference.jpg"))
        for index, path in enumerate(generated_images, 1):
            shutil.copy2(path, product_root / "images" / f"etsy_{index:02d}.png")
        customer_target = product_root / "files" / safe_name(customer_file.name, "customer.zip")
        shutil.copy2(customer_file, customer_target)
        validate_final_artifact(customer_target)
        atomic_json(product_root / "license_receipt.json", license_payload)
        atomic_json(product_root / "listing.json", listing_payload)
        return product_root, folder, number
    except Exception:
        shutil.rmtree(product_root, ignore_errors=True)
        raise


def create_workbook_row(shop_root: Path, folder: str, listing: dict[str, Any]) -> tuple[int, Path]:
    if openpyxl is None:
        raise BotError("Thiếu openpyxl để ghi Etsy workbook")
    workbook = shop_root / "Etsy_SEO_Generator.xlsx"
    backup = shop_root / f"Etsy_SEO_Generator.bot_backup_{stamp()}.xlsx"
    shutil.copy2(workbook, backup)
    wb = openpyxl.load_workbook(workbook)
    try:
        ws = wb["Listings"]
        row_number = None
        for row in range(4, max(ws.max_row + 2, 5)):
            if not str(ws.cell(row=row, column=2).value or "").strip():
                row_number = row
                break
        if row_number is None:
            row_number = ws.max_row + 1
        values = {
            2: folder,
            3: listing.get("keywords", ""),
            5: float(listing["price"]),
            6: listing.get("category", "Digital"),
            8: listing["title"],
            9: listing["description"],
            10: ", ".join(listing["tags"]),
            11: int(listing.get("qty", 999)),
            13: listing.get("when_made", "2020_2026"),
            14: "⏳ Chờ đăng",
            15: listing.get("section", "Digital Planner"),
            16: "",
            18: f"DAISY-{folder.split('-')[-1]}",
        }
        for column, value in values.items():
            ws.cell(row=row_number, column=column, value=value)
        wb.save(workbook)
    except Exception:
        try:
            shutil.copy2(backup, workbook)
        finally:
            wb.close()
        raise
    else:
        wb.close()
    return row_number, backup


def create_pipeline_job(product_root: Path, folder: str, shop_id: str, source_url: str, license_payload: dict[str, Any]) -> dict[str, Any]:
    stage_dir = product_root / "source_assets"
    fingerprint = source_fingerprint(stage_dir)
    run_id = product_root.name + "-" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
    candidate_id = "cf-" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:20]
    store = JobStore(JOB_DB)
    try:
        job, _created = store.create_pipeline_job(
            campaign_id=run_id,
            shop_id=shop_id,
            stage="create-verified-draft",
            product_id=folder,
            candidate_id=candidate_id,
            input_version="1",
            source_fingerprint=fingerprint,
            payload={"source_url": source_url, "license": license_payload.get("license"), "folder": folder},
            side_effect_boundary="etsy_write",
            max_inflight_jobs=10,
        )
        job_id = str(job["job_id"])
        claimed = store.claim_pipeline_job(job_id, worker_id=WORKER_ID, lease_seconds=1800)
        if not claimed:
            raise BotError(f"Không claim được pipeline job: {job_id}")
        manifest = write_pipeline_product_manifest(
            product_root,
            campaign_id=str(claimed["campaign_id"]),
            candidate_id=str(claimed["candidate_id"]),
            product_id=folder,
            pipeline_job_id=job_id,
            shop_id=shop_id,
            worker_id=WORKER_ID,
            customer_files=[path.relative_to(product_root) for path in (product_root / "files").iterdir() if path.is_file()],
            image_files=[path.relative_to(product_root) for path in sorted((product_root / "images").iterdir()) if path.is_file()],
            source_assets=[path.relative_to(product_root) for path in sorted((product_root / "source_assets").rglob("*")) if path.is_file()],
            source_provenance=[
                {
                    "url": source_url,
                    "license": str(license_payload.get("license")),
                    "resale_allowed": True,
                    "checked_at": str(license_payload.get("checked_at") or utc_now()),
                }
            ],
        )
        manifest_path = product_root / ".pipeline-product.json"
        store.record_pipeline_manifest(
            job_id,
            worker_id=WORKER_ID,
            fence=int(claimed["fence"]),
            manifest_sha256=sha256_file(manifest_path),
            manifest=manifest,
        )
        return {"job_id": job_id, "campaign_id": claimed["campaign_id"], "fence": claimed["fence"], "manifest": manifest}
    finally:
        store.close()


def run_existing_poster(shop_id: str, row: int, folder: str, run_dir: Path) -> dict[str, Any]:
    if not POSTER.is_file():
        raise BotError(f"Không tìm thấy Etsy poster: {POSTER}")
    env = dict(os.environ)
    env.update({"ETSY_PIPELINE_WORKER_ID": WORKER_ID, "PYTHONUNBUFFERED": "1"})
    command = [sys.executable, "-u", str(POSTER), "--shop", shop_id, "--selected-product", f"{row}:{folder}"]
    result = subprocess.run(command, cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=1800)
    log_path = run_dir / "etsy-poster.log"
    log_path.write_text((result.stdout or "") + "\n--- STDERR ---\n" + (result.stderr or ""), encoding="utf-8")
    if result.returncode != 0:
        raise BotError(f"Etsy poster thất bại exit={result.returncode}; xem {log_path}")
    store = JobStore(JOB_DB)
    try:
        # The poster is responsible for fenced completion. Read it back instead
        # of trusting stdout or the subprocess exit code alone.
        jobs = store.list_pipeline_jobs(shop_id=shop_id, stage="create-verified-draft", limit=50)
        matching = [job for job in jobs if str(job.get("product_id")) == folder]
        if not matching:
            raise BotError(f"Không đọc lại được JobStore cho {folder}")
        job = matching[0]
        if job.get("status") != "verified":
            raise BotError(f"Draft chưa ở trạng thái verified: {job.get('status')}")
        receipt = job.get("receipt") if isinstance(job.get("receipt"), dict) else {}
        listing_id = str(receipt.get("listing_id") or "").strip()
        readback_url = str(receipt.get("readback_url") or "").strip()
        if not listing_id or not readback_url:
            raise BotError("Draft verified nhưng thiếu listing_id/readback_url")
        return {"job": job, "listing_id": listing_id, "readback_url": readback_url, "log": str(log_path)}
    finally:
        store.close()


def update_workbook_status(shop_root: Path, row: int, status: str, url: str | None = None) -> None:
    if openpyxl is None:
        raise BotError("Thiếu openpyxl khi cập nhật trạng thái workbook")
    workbook = shop_root / "Etsy_SEO_Generator.xlsx"
    wb = openpyxl.load_workbook(workbook)
    try:
        ws = wb["Listings"]
        ws.cell(row=row, column=14, value=status)
        if url:
            ws.cell(row=row, column=16, value=url)
        wb.save(workbook)
    finally:
        wb.close()


def publish_listing(shop_id: str, listing_id: str, title: str) -> dict[str, Any]:
    cfg = read_shop_config(shop_id)
    shops = cfg["all"]
    session = resolve_etsy_session(ROOT, shops, shop_id)
    if not is_session_ready(session):
        raise BotError(
            f"Etsy session chưa sẵn sàng cho {shop_id}; mở đúng profile {session.profile_dir} "
            f"(CDP {session.debug_port}) và hoàn tất login trong UI"
        )
    edit_url = f"https://www.etsy.com/your/shops/me/listing-editor/edit/{listing_id}"
    with connected_browser(session.cdp_url) as (_browser, context):
        page = pick_page(context, "etsy.com")
        page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        if "/signin" in str(page.url).lower() or "/join" in str(page.url).lower():
            raise BotError("Etsy session chuyển sang login; chưa publish")
        expected_slug = str(cfg.get("etsy_link") or "").rstrip("/").split("/shop/")[-1].lower()
        hrefs = page.locator('a[href*="/shop/"]').evaluate_all(
            "els => els.map(a => a.href || a.getAttribute('href') || '')"
        )
        observed = {re.split(r"[/?#]", str(h).split("/shop/")[-1].lower())[0] for h in (hrefs or []) if "/shop/" in str(h)}
        if expected_slug and expected_slug not in observed and expected_slug not in str(page.url).lower():
            raise BotError(f"Không xác minh được đúng Etsy shop {shop_id}; thấy {sorted(observed)}")

        buttons = page.locator("button:visible")
        publish_button = None
        for index in range(await_count(buttons)):
            button = buttons.nth(index)
            text = re.sub(r"\s+", " ", button.inner_text()).strip().casefold()
            if text in {"publish", "publish listing", "publish changes"}:
                publish_button = button
                break
        if publish_button is None:
            raise BotError("Không tìm thấy nút Publish trong Etsy editor; giữ draft để không đoán UI")
        publish_button.click()
        page.wait_for_timeout(5000)
        # Some Etsy versions show a second confirmation button.
        buttons = page.locator("button:visible")
        for index in range(await_count(buttons)):
            button = buttons.nth(index)
            text = re.sub(r"\s+", " ", button.inner_text()).strip().casefold()
            if text in {"publish", "publish listing", "publish changes"}:
                try:
                    button.click()
                    page.wait_for_timeout(5000)
                    break
                except Exception:
                    pass

        public_url = f"https://www.etsy.com/listing/{listing_id}"
        page.goto(public_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        body = page_body(page)
        if title.casefold()[:40] not in body.casefold():
            raise BotError("Publish click đã chạy nhưng public Etsy readback chưa thấy đúng title")
        receipt = {
            "schema": "creativeca-etsy-publish.v1",
            "shop_id": shop_id,
            "listing_id": listing_id,
            "public_url": public_url,
            "title_readback": title,
            "published_at": utc_now(),
            "readback_ok": True,
        }
        return receipt


def build_run_dir(run_id: str | None) -> Path:
    value = str(run_id or stamp()).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value):
        raise BotError("run-id chỉ được chứa chữ, số, ., _, -")
    path = RUN_ROOT / value
    if path.exists():
        raise BotError(f"Run directory đã tồn tại; dùng --run-id khác để tránh trộn checkpoint: {path}")
    path.mkdir(parents=True, exist_ok=False)
    return path


def validate_inputs(args) -> tuple[str, dict[str, Any], dict[str, Any], str, Path]:
    shop_id = str(args.shop).strip()
    read_shop_config(shop_id)
    if args.source_url:
        source_url = validate_creative_fabrica_url(args.source_url)
    else:
        source_url = ""
    if not source_url and not args.source_file:
        raise BotError("Cần --source-url hoặc --source-file")
    if args.source_file:
        source_file = _require_file(Path(args.source_file), "source-file")
        if source_file.suffix.lower() != ".zip":
            raise BotError("source-file hiện phải là ZIP")
    else:
        source_file = Path()
    license_path = Path(args.license_json)
    if source_url:
        license_payload = load_license(license_path, source_url)
    else:
        raw_license = load_json(license_path, "license.json")
        declared_source = validate_creative_fabrica_url(str(raw_license.get("source_url") or ""))
        license_payload = load_license(license_path, declared_source)
        source_url = declared_source
    listing_payload = validate_listing_payload(load_json(Path(args.listing_json), "listing.json"), source_url)
    prompt = load_prompt(Path(args.prompt_file))
    customer_file = _require_file(Path(args.customer_file), "customer-file")
    if customer_file.suffix.lower() not in CUSTOMER_EXTS:
        raise BotError("customer-file phải là PDF hoặc ZIP")
    return source_url, license_payload, listing_payload, prompt, customer_file


def run(args) -> dict[str, Any]:
    require_runtime()
    source_url, license_payload, listing_payload, prompt, customer_file = validate_inputs(args)
    run_dir = build_run_dir(args.run_id)
    checkpoint(run_dir, "validated", shop_id=args.shop, source_url=source_url, customer_file=str(customer_file))
    if args.dry_run:
        result = {
            "ok": True,
            "dry_run": True,
            "run_dir": str(run_dir),
            "shop_id": args.shop,
            "source_url": source_url,
            "license": license_payload.get("license"),
            "listing_title": listing_payload["title"],
            "prompt_chars": len(prompt),
            "customer_file": str(customer_file),
        }
        atomic_json(run_dir / "dry-run.json", result)
        return result

    stage_dir = run_dir / "stage"
    if args.source_file:
        source_assets = stage_dir / "source_assets"
        source_assets.mkdir(parents=True, exist_ok=True)
        staged_zip = source_assets / safe_name(Path(args.source_file).name, "source.zip")
        shutil.copy2(customer_file if Path(args.source_file).resolve() == customer_file.resolve() else Path(args.source_file), staged_zip)
        with zipfile.ZipFile(staged_zip) as archive:
            if not archive.namelist():
                raise BotError("source-file ZIP không có member")
        download_result = {"zip": staged_zip, "references": [], "receipt": {"source_file": str(staged_zip)}}
    else:
        download_result = creative_fabrica_download(source_url, stage_dir, args.cf_cdp_url)
    chatgpt_source = prepare_chatgpt_source(
        download_result["zip"], stage_dir, download_result["references"]
    )
    checkpoint(
        run_dir,
        "downloaded",
        shop_id=args.shop,
        source_url=source_url,
        source_zip=str(download_result["zip"]),
        chatgpt_source=str(chatgpt_source),
        chatgpt_source_bytes=chatgpt_source.stat().st_size,
        reference_count=len(download_result["references"]),
    )

    generated = generate_chatgpt_images(
        stage_dir,
        chatgpt_source,
        [],
        prompt,
        args.chatgpt_cdp_url,
        args.gpt_url,
        timeout_seconds=args.image_timeout,
    )
    checkpoint(run_dir, "images_generated", generated_count=len(generated), generated=[str(path) for path in generated])
    validate_image_set(generated)

    shop_root = ROOT / "shops" / args.shop
    product_root, folder, product_number = materialize_product(
        shop_root,
        stage_dir,
        download_result["zip"],
        download_result["references"],
        generated,
        customer_file,
        license_payload,
        listing_payload,
    )
    try:
        row, backup = create_workbook_row(shop_root, folder, listing_payload)
        pipeline = create_pipeline_job(product_root, folder, args.shop, source_url or str(license_payload.get("source_url") or "source-file"), license_payload)
        checkpoint(
            run_dir,
            "product_materialized",
            shop_id=args.shop,
            product_folder=folder,
            product_root=str(product_root),
            workbook_row=row,
            workbook_backup=str(backup),
            pipeline_job_id=pipeline["job_id"],
        )
    except Exception:
        shutil.rmtree(product_root, ignore_errors=True)
        raise

    draft = run_existing_poster(args.shop, row, folder, run_dir)
    checkpoint(
        run_dir,
        "draft_verified",
        product_folder=folder,
        workbook_row=row,
        pipeline_job_id=pipeline["job_id"],
        listing_id=draft["listing_id"],
        readback_url=draft["readback_url"],
    )
    result: dict[str, Any] = {
        "ok": True,
        "run_dir": str(run_dir),
        "shop_id": args.shop,
        "product_folder": folder,
        "workbook_row": row,
        "pipeline_job_id": pipeline["job_id"],
        "listing_id": draft["listing_id"],
        "draft_url": draft["readback_url"],
        "draft_verified": True,
        "published": False,
    }
    update_workbook_status(shop_root, row, "✅ Đã đăng draft", draft["readback_url"])
    if args.publish:
        if str(args.confirm_publish or "") != args.shop:
            raise BotError("Publish yêu cầu --confirm-publish đúng bằng shop id; draft vẫn đã được xác minh")
        publish_receipt = publish_listing(args.shop, draft["listing_id"], listing_payload["title"])
        atomic_json(run_dir / "publish-receipt.json", publish_receipt)
        update_workbook_status(shop_root, row, "✅ Đã đăng", publish_receipt["public_url"])
        result.update({"published": True, "public_url": publish_receipt["public_url"], "publish_readback_ok": True})
        checkpoint(run_dir, "published", **result)
    else:
        checkpoint(run_dir, "draft_verified", **result)
    atomic_json(run_dir / "result.json", result)
    return result


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--shop", default="daisyflowdigital")
    command.add_argument("--source-url", default="", help="Creative Fabrica product URL")
    command.add_argument("--source-file", default="", help="Local ZIP for a no-download/resume test")
    command.add_argument("--cf-cdp-url", default=os.environ.get("CREATIVE_FABRICA_CDP_URL", ""))
    command.add_argument("--chatgpt-cdp-url", default=DEFAULT_CHATGPT_CDP)
    command.add_argument("--gpt-url", default=DEFAULT_GPT_URL)
    command.add_argument("--prompt-file", required=True)
    command.add_argument("--listing-json", required=True)
    command.add_argument("--license-json", required=True)
    command.add_argument("--customer-file", required=True)
    command.add_argument("--run-id", default=None)
    command.add_argument("--image-timeout", type=int, default=900)
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--publish", action="store_true", help="Publish after verified draft; otherwise stop at draft")
    command.add_argument("--confirm-publish", default="", help="Must equal --shop to cross the live publish gate")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.source_file and not args.cf_cdp_url and not args.dry_run:
        print("ERROR: --cf-cdp-url bắt buộc khi tải trực tiếp Creative Fabrica", file=sys.stderr)
        return 2
    try:
        result = run(args)
    except (BotError, PipelineContractError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
