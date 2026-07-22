import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = ctx.pages[0]
        
        body_text = await page.evaluate("() => document.body.innerText")
        lines = body_text.split('\n')
        tags_line = None
        for line in lines:
            if ',' in line:
                tokens = line.split(',')
                if len(tokens) >= 5 and all(len(t.strip()) <= 35 for t in tokens):
                    tags_line = line
                    break
                    
        print("FOUND TAGS:", repr(tags_line))

asyncio.run(main())
