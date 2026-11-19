#!/usr/bin/env python3
"""Sequentially regenerate BubbleByJulie listing images through ChatGPT.

The worker is deliberately fail-closed: source images and generated assets are
validated before any replacement, and state is checkpointed atomically.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterable

from PIL import Image
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]
SHOP_ROOT = REPO_ROOT / "shops" / "BubbleByJulie"
PROMPT_PATH = REPO_ROOT / "scripts" / "bubble_redesign_prompt.txt"
STATE_PATH = SHOP_ROOT / "bubble_redesign_57_state.json"
LOG_PATH = REPO_ROOT / "bubble_redesign_57.log"
CDP_URL = "http://127.0.0.1:9333"
MAX_WAIT_SECONDS = 900

PRODUCTS = tuple(
    [f"product-{n:02d}" for n in range(7, 10)]
    + ["product-12"]
    + [f"product-{n:02d}" for n in range(14, 65)]
    + ["product-174", "product-175"]
)
assert len(PRODUCTS) == 57 and len(set(PRODUCTS)) == 57


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("bubble_redesign_57")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.addHandler(logging.StreamHandler())
    return logger


def atomic_json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "products": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read checkpoint: {exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("products", {}), dict):
        raise RuntimeError("checkpoint has an invalid shape")
    return state


def png_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".png")


def read_png(path: Path) -> tuple[int, int]:
    """Decode the image and require a real, non-empty PNG."""
    with Image.open(path) as image:
        if image.format != "PNG":
            raise ValueError(f"{path.name} is not PNG")
        image.load()
        width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError(f"{path.name} has invalid dimensions ({width}x{height})")
    return width, height


def validate_png(path: Path) -> tuple[int, int]:
    """Decode a generated PNG and require a square image."""
    width, height = read_png(path)
    if width != height:
        raise ValueError(f"{path.name} is not square ({width}x{height})")
    return width, height


def validate_set(files: Iterable[Path], expected: int = 10) -> str:
    paths = list(files)
    if len(paths) != expected:
        raise ValueError(f"expected {expected} files, found {len(paths)}")
    hashes: set[str] = set()
    for path in paths:
        validate_png(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in hashes:
            raise ValueError("generated files are not distinct")
        hashes.add(digest)
    return hashlib.sha256("".join(sorted(hashes)).encode()).hexdigest()


def product_prompt(product_dir: Path, base_prompt: str) -> str:
    source_path = product_dir / "source_catalog" / "source.json"
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{product_dir.name}: cannot read source JSON: {exc}") from exc
    details = source.get("english_product_details")
    dimensions = details.get("standard_dimensions") if isinstance(details, dict) else None
    dimensions = dimensions.strip() if isinstance(dimensions, str) else ""
    parsed = re.search(
        r"W\s*([0-9]+(?:\.[0-9]+)?)\s*x\s*D\s*([0-9]+(?:\.[0-9]+)?)\s*x\s*H\s*([0-9]+(?:\.[0-9]+)?)\s*(mm|cm|in|\")",
        dimensions,
        flags=re.IGNORECASE,
    )
    if not parsed:
        missing = "[NO VERIFIED DIMENSIONS — OMIT ALL DIMENSION GRAPHICS AND TEXT]"
        return (
            base_prompt.replace("[REPLACE WITH VERIFIED DIMENSIONS]", missing)
            .replace("[INSERT DIMENSIONS]", missing)
            .replace("[VERIFIED_WIDTH_LABEL]", "[OMIT WIDTH LABEL]")
            .replace("[VERIFIED_DEPTH_LABEL]", "[OMIT DEPTH LABEL]")
            .replace("[VERIFIED_HEIGHT_LABEL]", "[OMIT HEIGHT LABEL]")
        )
    width, depth, height, unit = parsed.groups()
    unit = unit.lower()
    dimensions = dimensions.strip()
    prompt = base_prompt.replace("[REPLACE WITH VERIFIED DIMENSIONS]", dimensions)
    prompt = prompt.replace("[INSERT DIMENSIONS]", dimensions)
    prompt = prompt.replace("W350 × D420 × H480 mm", dimensions)
    prompt = prompt.replace("[VERIFIED_WIDTH_LABEL]", f"W{width} {unit}")
    prompt = prompt.replace("[VERIFIED_DEPTH_LABEL]", f"D{depth} {unit}")
    prompt = prompt.replace("[VERIFIED_HEIGHT_LABEL]", f"H{height} {unit}")
    return prompt


def estuary_assets(page: Page) -> list[tuple[str, str]]:
    sources = page.evaluate(
        """() => {
          const images = [...document.querySelectorAll('img')]
          const generated = images.filter(img =>
            (img.alt || '').toLowerCase().includes('generated image')
          )
          const candidates = generated.length ? generated : images
          return candidates
            .map(img => img.currentSrc || img.src || '')
            .filter(src => src.includes('/backend-api/estuary/content'))
        }"""
    )
    assets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source in sources:
        match = re.search(r"[?&]id=([^&]+)", source)
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            assets.append((match.group(1), source))
    return assets


def generated_assets(page: Page) -> list[tuple[str, str]]:
    sources = page.evaluate(
        """() => [...document.querySelectorAll('img')]
          .filter(img => (img.alt || '').toLowerCase().includes('generated image'))
          .map(img => img.currentSrc || img.src || '')
          .filter(src => src.includes('/backend-api/estuary/content'))"""
    )
    assets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source in sources:
        match = re.search(r"[?&]id=([^&]+)", source)
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            assets.append((match.group(1), source))
    return assets


def visible_stop_button(page: Page) -> bool:
    locator = page.locator('button[aria-label*="Stop" i]')
    for index in range(locator.count()):
        try:
            if locator.nth(index).is_visible():
                return True
        except PlaywrightTimeoutError:
            pass
    return False


def prepare_page(context) -> Page:
    page = context.new_page()
    page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_selector("#prompt-textarea", state="visible", timeout=120_000)
    return page


def select_create_image(page: Page) -> None:
    page.locator('button[data-testid="composer-plus-btn"]').click(timeout=30_000)
    page.get_by_text("Create image", exact=True).last.click(timeout=30_000)


def generate_one(page: Page, source_files: list[Path], prompt: str, logger: logging.Logger) -> list[tuple[str, str]]:
    select_create_image(page)
    page.locator("#upload-files").set_input_files([str(path) for path in source_files])
    uploaded = page.locator("#upload-files").evaluate("node => node.files.length")
    if uploaded != 10:
        raise RuntimeError(f"upload verification failed: {uploaded} files")
    composer = page.locator("#prompt-textarea")
    try:
        composer.fill(prompt, timeout=120_000)
    except PlaywrightTimeoutError:
        logger.info("composer fill timed out; using keyboard text insertion fallback")
        composer.click(timeout=30_000)
        page.keyboard.press("Meta+A")
        page.keyboard.insert_text(prompt)
    entered = composer.evaluate(
        "node => ('value' in node ? node.value : (node.innerText || node.textContent || ''))"
    )
    normalized_entered = re.sub(r"\s+", "", entered)
    normalized_prompt = re.sub(r"\s+", "", prompt)
    if normalized_entered != normalized_prompt:
        raise RuntimeError("prompt verification failed; refusing to send")
    page.locator('button[aria-label="Send prompt"]').last.click(timeout=30_000)
    logger.info("prompt sent; prompt_sha256=%s", hashlib.sha256(prompt.encode()).hexdigest())

    # ChatGPT may materialize uploaded attachments as Estuary assets only after
    # the message is sent. Capture that post-send baseline before generation
    # outputs are added, instead of counting the source thumbnails as results.
    time.sleep(5)
    before_ids = {asset_id for asset_id, _ in estuary_assets(page)}
    logger.info("post-send baseline_estuary_assets=%d", len(before_ids))

    deadline = time.monotonic() + MAX_WAIT_SECONDS
    saw_stop = False
    quiet_polls = 0
    full_output_polls = 0
    while time.monotonic() < deadline:
        stopping = visible_stop_button(page)
        generated = generated_assets(page)
        if len(generated) == 10:
            logger.info("captured 10 generated image assets before gallery collapse")
            return generated
        assets = estuary_assets(page)
        count = len(assets)
        logger.info("poll generated_estuary_assets=%d stop=%s", count, stopping)
        saw_stop |= stopping
        if count == 10:
            full_output_polls += 1
            if full_output_polls >= 3:
                return [asset for asset in assets if asset[0] not in before_ids]
        else:
            full_output_polls = 0
        if saw_stop and not stopping:
            quiet_polls += 1
            if quiet_polls >= 2:
                return [
                    asset for asset in assets if asset[0] not in before_ids
                ]
        else:
            quiet_polls = 0
        time.sleep(5)
    raise TimeoutError(f"image generation exceeded {MAX_WAIT_SECONDS} seconds")


def replace_after_validation(product_dir: Path, assets: list[tuple[str, str]], context, logger: logging.Logger) -> str:
    staging = product_dir / ".bubble_redesign_57_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        for index, (_asset_id, url) in enumerate(assets, 1):
            response = context.request.get(url, timeout=180_000)
            if response.status != 200 or not response.body():
                raise RuntimeError(f"asset download failed ({response.status})")
            (staging / f"etsy_{index:02d}.png").write_bytes(response.body())
        set_hash = validate_set(sorted(staging.glob("*.png")))
        destination = product_dir / "images"
        destination.mkdir(exist_ok=True)
        old_files = list(destination.iterdir())
        for old in old_files:
            if old.is_file() or old.is_symlink():
                old.unlink()
            elif old.is_dir():
                shutil.rmtree(old)
        for generated in sorted(staging.glob("*.png")):
            os.replace(generated, destination / generated.name)
        logger.info("validated and replaced images set_sha256=%s", set_hash)
        return set_hash
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    logger = configure_logging()
    if not PROMPT_PATH.is_file():
        logger.error("prompt file missing: %s", PROMPT_PATH)
        raise SystemExit(f"prompt file missing: {PROMPT_PATH}")
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    state = load_state()
    state.setdefault("version", 1)
    state.setdefault("products", {})
    state["products"] = {name: state["products"].get(name, {}) for name in PRODUCTS}
    atomic_json_write(STATE_PATH, state)

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError("CDP browser has no contexts")
        context = browser.contexts[0]
        for product_name in PRODUCTS:
            record = state["products"][product_name]
            if record.get("status") == "done":
                logger.info("skip checkpointed product=%s", product_name)
                continue
            product_dir = SHOP_ROOT / product_name
            source_dir = product_dir / "images"
            source_files = png_files(source_dir) if source_dir.is_dir() else []
            if len(source_files) != 10:
                raise RuntimeError(f"{product_name}: expected exactly 10 current PNG files, found {len(source_files)}")
            # Validate source readability before opening a browser conversation.
            for source in source_files:
                read_png(source)
            record.update({"status": "running", "source_files": [p.name for p in source_files]})
            atomic_json_write(STATE_PATH, state)
            logger.info("start product=%s", product_name)
            page = prepare_page(context)
            try:
                current_prompt = product_prompt(product_dir, prompt)
                new_assets = generate_one(page, source_files, current_prompt, logger)
                if len({asset_id for asset_id, _ in new_assets}) != 10 or len(new_assets) != 10:
                    raise RuntimeError(f"expected exactly 10 new separate Estuary assets, found {len(new_assets)}")
                set_hash = replace_after_validation(product_dir, new_assets, context, logger)
                record.update({"status": "done", "asset_ids": [a for a, _ in new_assets], "set_sha256": set_hash})
                atomic_json_write(STATE_PATH, state)
                logger.info("done product=%s", product_name)
            except Exception as exc:
                record.update({"status": "failed", "error": str(exc)[:500]})
                atomic_json_write(STATE_PATH, state)
                logger.exception("failed product=%s", product_name)
                raise
            finally:
                page.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
