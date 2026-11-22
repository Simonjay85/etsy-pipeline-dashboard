from __future__ import annotations
import hashlib
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
import bubble_redesign_57 as worker

OUT = worker.REPO_ROOT / "output" / "bubble_redesign_product08_test"
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(worker.CDP_URL)
    pages = [pg for pg in browser.contexts[0].pages if "chatgpt.com/c/" in pg.url]
    if not pages:
        raise SystemExit("no ChatGPT conversation page")
    page = pages[-1]
    buttons = page.locator('button[aria-label^="Open image "]')
    if buttons.count() < 10:
        raise SystemExit(f"expected 10 gallery buttons, found {buttons.count()}")
    run_dir = OUT / f"run-{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=False)
    urls = []
    for index in range(10):
        buttons.nth(index).click(timeout=30_000)
        page.wait_for_timeout(500)
        image = page.locator('img[alt="Generated image"]').last
        image.wait_for(state="visible", timeout=30_000)
        url = image.get_attribute("src") or image.get_attribute("currentSrc")
        if not url or "/backend-api/estuary/content" not in url:
            raise SystemExit(f"image {index + 1} has no Estuary URL")
        urls.append(url)
    ids = []
    for url in urls:
        match = re.search(r"[?&]id=([^&]+)", url)
        if not match:
            raise SystemExit("gallery URL has no asset id")
        ids.append(match.group(1))
    if len(set(ids)) != 10:
        raise SystemExit("gallery URLs are not distinct")
    for index, url in enumerate(urls, 1):
        response = page.context.request.get(url, timeout=180_000)
        if response.status != 200 or not response.body():
            raise SystemExit(f"download failed for image {index}: {response.status}")
        (run_dir / f"etsy_{index:02d}.png").write_bytes(response.body())
    digest = worker.validate_set(sorted(run_dir.glob("*.png")))
    print(f"STAGED {run_dir} set_sha256={digest}")
    browser.close()
