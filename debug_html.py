"""Debug: dump HTML từ trang Etsy listings để xem cấu trúc thật"""
import asyncio, re
from playwright.async_api import async_playwright

URLS_TO_TRY = [
    "https://www.etsy.com/your/listings",
    "https://www.etsy.com/your/shops/me/tools/listings",
]

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx  = browser.contexts[0]

        # Tìm tab Etsy
        page = None
        for p in ctx.pages:
            if "etsy.com" in p.url and "signin" not in p.url:
                page = p
                break
        if not page:
            page = await ctx.new_page()

        for test_url in URLS_TO_TRY:
            print(f"\n{'='*60}")
            print(f"📍 Testing: {test_url}")
            await page.goto(test_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            print(f"  ➡️  Redirected to: {page.url[:100]}")

            html = await page.content()

            # Tìm listing IDs
            ids = re.findall(r'/listings/(\d{7,12})', html)
            unique = list(dict.fromkeys(ids))
            print(f"  🆔 Listing IDs found: {len(unique)} → {unique[:5]}")

            # Dump 500 chars xung quanh chữ "listings"
            idx = html.find('/listings/')
            if idx > 0:
                print(f"\n  📝 HTML snippet (xung quanh /listings/):")
                print(html[max(0,idx-50):idx+200])

            # Lưu HTML ra file
            with open(f"/tmp/etsy_page_{test_url.split('/')[-1]}.html", "w") as f:
                f.write(html)
            print(f"\n  💾 Full HTML saved to /tmp/etsy_page_{test_url.split('/')[-1]}.html")

asyncio.run(main())
