import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = ctx.pages[0]
        
        await page.goto("https://www.etsy.com/your/shops/me/tools/listings?ref=listings_manager_prototype")
        await page.wait_for_timeout(3000)
        
        # Get all links
        links = await page.evaluate("() => Array.from(document.querySelectorAll('a')).map(a => a.href).filter(href => href.includes('listing'))")
        for link in set(links):
            print("LINK:", link)

asyncio.run(main())
