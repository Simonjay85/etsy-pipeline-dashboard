import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = ctx.pages[0]
        
        html = await page.evaluate('''() => {
            let els = Array.from(document.querySelectorAll('p, span, div, li'));
            let tagEl = els.find(e => e.innerText && e.innerText.includes('landscape planner') && e.innerText.includes('ipad planner'));
            if (tagEl) {
                // Return parent tree structure to understand
                let current = tagEl;
                let path = [];
                for(let i=0; i<3 && current; i++) {
                    path.push(current.outerHTML);
                    current = current.parentElement;
                }
                return path[0];
            }
            return "NOT FOUND";
        }''')
        print("TAG EL HTML:", html)

asyncio.run(main())
