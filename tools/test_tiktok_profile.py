import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser=await pw.chromium.connect_over_cdp('http://127.0.0.1:9223')
        ctx=browser.contexts[0]
        page=await ctx.new_page()
        await page.goto('https://www.tiktok.com/@ririii77',wait_until='domcontentloaded',timeout=60000)
        await page.wait_for_timeout(5000)
        print(json.dumps({'url':page.url,'title':await page.title(),'text':(await page.locator('body').inner_text())[:1000],'images':await page.locator('img').count()},ensure_ascii=False))
        await page.close()

asyncio.run(main())
