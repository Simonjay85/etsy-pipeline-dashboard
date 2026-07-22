import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = ctx.pages[0]
        
        # Check the tags string
        tags_text = await page.evaluate("() => { let t = Array.from(document.querySelectorAll('p, span, div')).map(e => e.innerText).filter(t => t && t.includes('landscape planner')); return t[0] }")
        print("TAGS TEXT:", repr(tags_text))
        
        # Check publish button
        html = await page.evaluate("() => { const b = Array.from(document.querySelectorAll('button')).find(e => e.innerText && e.innerText.includes('Publish changes')); return b ? b.outerHTML : 'NOT FOUND' }")
        print("PUBLISH BUTTON HTML:", html)

asyncio.run(main())
