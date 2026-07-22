"""Test fill_translation với 1 listing + Dutch để verify title/desc/tags"""
import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

import sys
sys.path.insert(0, '/Users/aaronnguyen/Documents/Claude/Projects/Etsy')
from etsy_translate import fill_translation, STEALTH_JS

LISTING_ID = "4428935365"
CACHE_FILE = Path("/Users/aaronnguyen/Documents/Claude/Projects/Etsy/translations_cache.json")

async def main():
    cache = json.loads(CACHE_FILE.read_text())
    key = next(iter(cache))
    dutch = cache[key].get("Dutch", {})
    title = dutch.get("title", "")
    desc  = dutch.get("description", "")
    tags  = dutch.get("tags", [])
    print(f"📋 Listing: {LISTING_ID}")
    print(f"🇳🇱 Title ({len(title)} chars): {title[:80]}")
    print(f"🇳🇱 Desc ({len(desc)} chars, has_newline={'chr(10)' in desc}): {desc[:60]}...")
    print(f"🇳🇱 Tags ({len(tags)}): {tags[:5]}")
    print()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "etsy.com" in p.url and "signin" not in p.url), None)
        if not page:
            page = await ctx.new_page()

        print("🚀 Bắt đầu fill Dutch translation (title + desc + tags)...")
        result = await fill_translation(page, LISTING_ID, "Dutch", title, desc, tags)
        print(f"\n{'✅ Thành công!' if result else '❌ Thất bại'}")

asyncio.run(main())
