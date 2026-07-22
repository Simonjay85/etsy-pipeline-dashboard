"""
Sync eBay Seller Hub listings into Platform_Manager.xlsx.
Dùng: python3 ebay_shop_sync.py --site SITE_ID
"""
import argparse
import asyncio
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

DASH_DIR = Path(__file__).parent
BASE_DIR = DASH_DIR.parent
import sys
sys.path.insert(0, str(DASH_DIR))
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
EBAY_ACTIVE_URL = "https://www.ebay.com/sh/lst/active"


def ensure_deps():
    import sys
    for mod, pkg in [("openpyxl", "openpyxl"), ("playwright", "playwright")]:
        try:
            __import__(mod)
        except ImportError:
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"], check=True)


ensure_deps()

import openpyxl
from playwright.async_api import async_playwright
from excel_helpers import PRODUCT_COLS, products_from_excel, save_product_row, ensure_excel, set_cell
from ebay_auto_post import get_browser_context, load_site


def normalize_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def title_score(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


async def scrape_active_listings(page) -> list[dict]:
    await page.goto(EBAY_ACTIVE_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)
    items = await page.evaluate("""() => {
      const out = [];
      const links = Array.from(document.querySelectorAll('a[href*="/itm/"], a[href*="/lst/"]'));
      const seen = new Set();
      for (const a of links) {
        const href = a.href || '';
        const m = href.match(/\\/itm\\/(\\d+)/);
        if (!m) continue;
        const id = m[1];
        if (seen.has(id)) continue;
        seen.add(id);
        const title = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ');
        if (title.length < 5) continue;
        out.push({ id, title, url: href.split('?')[0] });
      }
      return out;
    }""")
    return items


def match_product(products: list[dict], listing: dict) -> dict | None:
    best, best_score = None, 0.0
    for p in products:
        score = title_score(p.get("title", ""), listing.get("title", ""))
        if score > best_score:
            best_score, best = score, p
    return best if best_score >= 0.75 else None


async def sync_shop(site_id: str):
    site = load_site(site_id)
    site_dir = BASE_DIR / "shops_wp" / site_id
    excel_path = site_dir / "Platform_Manager.xlsx"
    ensure_excel(excel_path)
    products = products_from_excel(site_dir, excel_path)

    pw = await async_playwright().start()
    ctx, _ = await get_browser_context(pw, site)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        listings = await scrape_active_listings(page)
        print(f"[eBay-SYNC] Found {len(listings)} listings on Seller Hub")
        matched = 0
        for listing in listings:
            product = match_product(products, listing)
            if product:
                save_product_row(excel_path, product["row"], {
                    "ebay_url": listing["url"],
                    "ebay_status": "active",
                })
                matched += 1
        print(f"[eBay-SYNC] Matched {matched} products in Excel")
    finally:
        await pw.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    args = parser.parse_args()
    asyncio.run(sync_shop(args.site))


if __name__ == "__main__":
    main()
