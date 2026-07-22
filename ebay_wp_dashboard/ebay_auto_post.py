"""
eBay Auto Listing Poster — Playwright browser automation.
Dùng: python3 ebay_auto_post.py --site SITE_ID [--row N] [--batch 5]
Safety: ALLOW_EBAY_POST=1 required.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

if os.environ.get("ALLOW_EBAY_POST") != "1":
    print("⛔ eBay auto-post is locked. Set ALLOW_EBAY_POST=1 when intentionally posting.")
    sys.exit(0)

DASH_DIR = Path(__file__).parent
BASE_DIR = DASH_DIR.parent
sys.path.insert(0, str(DASH_DIR))
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
EBAY_SELL_URL = "https://www.ebay.com/sh/lst/active"
EBAY_CREATE_URL = "https://www.ebay.com/sl/sell"


def ensure_deps():
    for mod, pkg in [("openpyxl", "openpyxl"), ("playwright", "playwright")]:
        try:
            __import__(mod)
        except ImportError:
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"], check=True)


ensure_deps()

import openpyxl
from playwright.async_api import async_playwright

from excel_helpers import PRODUCT_COLS, products_from_excel, save_product_row, set_cell, ensure_excel


def load_site(site_id: str) -> dict:
    cfg = json.loads((DASH_DIR / "ebay_wp_config.json").read_text(encoding="utf-8"))
    site = cfg.get(site_id, {})
    secrets_path = DASH_DIR / "ebay_wp_secrets.json"
    if secrets_path.exists():
        secrets = json.loads(secrets_path.read_text(encoding="utf-8")).get(site_id, {})
        site.update(secrets)
    return site


async def get_browser_context(pw, site: dict):
    raw_session = site.get("browser_session", "~/.ebay_browser_session")
    debug_port = int(site.get("debug_port", 9230))
    browser_dir = Path(raw_session.replace("~", str(Path.home())))

    try:
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{debug_port}", timeout=5000)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        return ctx, True
    except Exception as e:
        print(f"[eBay] CDP port {debug_port} unavailable: {e}")

    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (browser_dir / lock).unlink(missing_ok=True)
        except Exception:
            pass

    launch_kw = {
        "user_data_dir": str(browser_dir),
        "headless": False,
        "args": ["--start-maximized", "--disable-blink-features=AutomationControlled"],
        "viewport": None,
    }
    if CHROME_PATH.exists():
        launch_kw["executable_path"] = str(CHROME_PATH)
    ctx = await pw.chromium.launch_persistent_context(**launch_kw)
    return ctx, False


async def fill_listing(page, product: dict, site_dir: Path) -> str | None:
    """Create eBay listing draft. Returns listing URL if found."""
    await page.goto(EBAY_CREATE_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)

    title = (product.get("title") or "")[:80]
    for sel in [
        'input[name="title"]',
        '#title',
        '[data-testid="title-input"]',
        'input[aria-label*="Title"]',
        'textarea[name="title"]',
    ]:
        el = page.locator(sel).first
        if await el.count() > 0:
            await el.fill(title)
            break

    desc = product.get("description", "")
    for sel in [
        'iframe[title*="description"]',
        'iframe[id*="description"]',
        '[data-testid="description-editor"]',
        'textarea[name="description"]',
    ]:
        frame_el = page.locator(sel).first
        if await frame_el.count() > 0:
            tag = await frame_el.evaluate("el => el.tagName")
            if tag == "IFRAME":
                frame = await frame_el.content_frame()
                if frame:
                    body = frame.locator("body")
                    if await body.count() > 0:
                        await body.fill(desc)
                        break
            else:
                await frame_el.fill(desc)
                break

    price = str(product.get("price", "4.99"))
    for sel in [
        'input[name="price"]',
        '#price',
        '[data-testid="price-input"]',
        'input[aria-label*="Price"]',
    ]:
        el = page.locator(sel).first
        if await el.count() > 0:
            await el.fill(price)
            break

    img_dir = site_dir / product["folder"] / "images"
    if img_dir.exists():
        images = sorted(
            f for f in img_dir.iterdir()
            if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        )[:12]
        if images:
            for sel in [
                'input[type="file"][accept*="image"]',
                'input[type="file"]',
            ]:
                file_input = page.locator(sel).first
                if await file_input.count() > 0:
                    await file_input.set_input_files([str(p) for p in images[:12]])
                    await page.wait_for_timeout(2000)
                    break

    for btn_text in ("Save for later", "Save draft", "List item", "Continue"):
        btn = page.get_by_role("button", name=btn_text)
        if await btn.count() > 0:
            try:
                await btn.first.click(timeout=5000)
                await page.wait_for_timeout(2000)
            except Exception:
                pass

    url = page.url
    if "ebay.com" in url and ("/itm/" in url or "/lst/" in url or "/sl/" in url):
        return url
    return None


async def post_product(site_id: str, row: int) -> bool:
    site = load_site(site_id)
    site_dir = BASE_DIR / "shops_wp" / site_id
    excel_path = site_dir / "Platform_Manager.xlsx"
    ensure_excel(excel_path)
    products = products_from_excel(site_dir, excel_path)
    product = next((p for p in products if p["row"] == row), None)
    if not product:
        print(f"[eBay] Row {row} not found")
        return False

    save_product_row(excel_path, row, {"ebay_status": "posting"})

    pw = await async_playwright().start()
    ctx, _ = await get_browser_context(pw, site)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        listing_url = await fill_listing(page, product, site_dir)
        if listing_url:
            save_product_row(excel_path, row, {
                "ebay_status": "draft",
                "ebay_url": listing_url,
            })
            print(f"[eBay] ✅ Posted row {row}: {listing_url}")
            return True
        save_product_row(excel_path, row, {"ebay_status": "error", "extra": "No listing URL captured"})
        print(f"[eBay] ⚠ Row {row}: draft may be saved but URL not captured")
        return False
    except Exception as e:
        save_product_row(excel_path, row, {"ebay_status": "error", "extra": str(e)[:200]})
        print(f"[eBay] ❌ Row {row} error: {e}")
        return False
    finally:
        await pw.stop()


async def post_batch(site_id: str, batch: int = 5, skip: int = 0):
    site_dir = BASE_DIR / "shops_wp" / site_id
    excel_path = site_dir / "Platform_Manager.xlsx"
    products = products_from_excel(site_dir, excel_path)
    pending = [p for p in products if p["ebay_status"] in ("pending", "error")]
    pending = pending[skip:skip + batch]
    print(f"[eBay] Posting {len(pending)} products...")
    for p in pending:
        await post_product(site_id, p["row"])
        await asyncio.sleep(3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--row", type=int)
    parser.add_argument("--batch", type=int, default=5)
    parser.add_argument("--skip", type=int, default=0)
    args = parser.parse_args()
    if args.row:
        asyncio.run(post_product(args.site, args.row))
    else:
        asyncio.run(post_batch(args.site, args.batch, args.skip))


if __name__ == "__main__":
    main()
