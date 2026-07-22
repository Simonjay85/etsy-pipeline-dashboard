import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = ctx.pages[0]
        
        # 1. Test reading tags
        raw_tags = await page.evaluate('''() => {
            let els = Array.from(document.querySelectorAll('p, span, div, text'));
            let tagText = els.map(e => e.innerText).find(t => t && t.includes(',') && t.split(',').length > 5 && t.length > 30);
            return tagText || "";
        }''')
        print("READ TAGS:", raw_tags)
        
        # 2. Test finding tags input for a specific language
        # Assume "Spanish" or whatever tab is active
        html = await page.evaluate('''() => {
            let inputs = Array.from(document.querySelectorAll('input[placeholder*="Shape"]'));
            return inputs.map(i => {
                let parentText = i.parentElement.parentElement.innerText;
                return {html: i.outerHTML, parentText: parentText};
            });
        }''')
        print("TAG INPUTS:", html)

asyncio.run(main())
