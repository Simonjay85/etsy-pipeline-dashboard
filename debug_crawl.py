"""
debug_crawl.py - Kiểm tra trang Etsy listings trả về gì
"""
import asyncio, re
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx  = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print("📍 Đang navigate đến Etsy listings...")
        await page.goto("https://www.etsy.com/your/listings", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        print(f"📄 URL hiện tại: {page.url}")
        print(f"📝 Title: {await page.title()}")

        # Tìm tất cả links có /listings/ trong href
        all_links = await page.locator('a[href*="listings"]').all()
        print(f"\n🔗 Tổng links có 'listings': {len(all_links)}")
        
        # In 10 link đầu
        for i, link in enumerate(all_links[:10]):
            href = await link.get_attribute("href") or ""
            text = (await link.inner_text())[:60]
            print(f"  [{i}] {href[:80]} | text: {text}")

        # Tìm edit links
        edit_links = await page.locator('a[href*="/edit"]').all()
        print(f"\n✏️  Links có '/edit': {len(edit_links)}")
        for i, link in enumerate(edit_links[:5]):
            href = await link.get_attribute("href") or ""
            print(f"  [{i}] {href[:100]}")

        # Dump phần HTML đầu
        content = await page.content()
        # Tìm listing IDs trong HTML
        ids = re.findall(r'/listings/(\d{9,12})', content)
        unique_ids = list(dict.fromkeys(ids))
        print(f"\n🆔 Listing IDs tìm thấy trong HTML: {len(unique_ids)}")
        for lid in unique_ids[:10]:
            print(f"  #{lid}")

        print("\n✅ Debug xong!")

asyncio.run(main())
