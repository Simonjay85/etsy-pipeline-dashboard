"""
Push updates to existing eBay listings via Playwright.
Dùng: python3 ebay_push_update.py --site SITE_ID --row N
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

if os.environ.get("ALLOW_EBAY_POST") != "1":
    print("⛔ eBay push is locked. Set ALLOW_EBAY_POST=1 when intentionally updating.")
    sys.exit(0)

DASH_DIR = Path(__file__).parent
BASE_DIR = DASH_DIR.parent
sys.path.insert(0, str(DASH_DIR))
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def ensure_deps():
    for mod, pkg in [("openpyxl", "openpyxl"), ("playwright", "playwright")]:
        try:
            __import__(mod)
        except ImportError:
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"], check=True)


ensure_deps()

from playwright.async_api import async_playwright
from excel_helpers import products_from_excel, save_product_row, ensure_excel
from ebay_auto_post import get_browser_context, load_site


async def push_product(site_id: str, row: int) -> bool:
    site = load_site(site_id)
    site_dir = BASE_DIR / "shops_wp" / site_id
    excel_path = site_dir / "Platform_Manager.xlsx"
    ensure_excel(excel_path)
    products = products_from_excel(site_dir, excel_path)
    product = next((p for p in products if p["row"] == row), None)
    if not product:
        print(f"[eBay-PUSH] Row {row} not found")
        return False
    if not product.get("ebay_url"):
        print(f"[eBay-PUSH] Row {row} has no eBay URL")
        return False

    pw = await async_playwright().start()
    ctx, _ = await get_browser_context(pw, site)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(product["ebay_url"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        for sel in ['input[name="title"]', '#title', 'input[aria-label*="Title"]']:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.fill((product["title"] or "")[:80])
                break

        price = str(product.get("price", "4.99"))
        for sel in ['input[name="price"]', '#price', 'input[aria-label*="Price"]']:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.fill(price)
                break

        for btn_text in ("Revise", "Save", "Update listing", "List item"):
            btn = page.get_by_role("button", name=btn_text)
            if await btn.count() > 0:
                try:
                    await btn.first.click(timeout=5000)
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass

        save_product_row(excel_path, row, {"ebay_status": "active"})
        print(f"[eBay-PUSH] ✅ Updated row {row}")
        return True
    except Exception as e:
        save_product_row(excel_path, row, {"ebay_status": "error", "extra": str(e)[:200]})
        print(f"[eBay-PUSH] ❌ Row {row}: {e}")
        return False
    finally:
        await pw.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--row", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(push_product(args.site, args.row))


if __name__ == "__main__":
    main()
