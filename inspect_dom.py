"""Inspect DOM structure quanh Dutch Title/Description/Tags để debug."""
import asyncio
from playwright.async_api import async_playwright

LISTING_ID = "4428935365"

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "etsy.com" in p.url), None)
        if not page:
            page = await ctx.new_page()

        # Navigate to listing editor
        url = f"https://www.etsy.com/your/shops/me/listing-editor/{LISTING_ID}"
        await page.goto(url, wait_until="domcontentloaded")

        # Scroll để load lazy sections
        for _ in range(12):
            await page.keyboard.press("PageDown")
            await page.wait_for_timeout(400)
        await page.wait_for_timeout(2000)

        # Dump context xung quanh "Dutch" headings
        result = await page.evaluate("""
            () => {
                const out = [];
                const all = [...document.querySelectorAll('*')];

                // Tìm tất cả nodes có text chứa "Dutch"
                const dutchNodes = all.filter(el =>
                    el.children.length === 0 &&
                    el.textContent.trim().startsWith('Dutch')
                );

                for (const hd of dutchNodes) {
                    const info = {
                        text: hd.textContent.trim(),
                        tag: hd.tagName,
                        parentTag: hd.parentElement?.tagName,
                        parentClass: hd.parentElement?.className?.substring(0, 60),
                        nextSiblings: [],
                        parentNextSiblings: [],
                    };

                    // Next siblings của heading
                    let sib = hd.nextElementSibling;
                    for (let i = 0; i < 5 && sib; i++) {
                        info.nextSiblings.push({
                            tag: sib.tagName,
                            hasTextarea: !!sib.querySelector('textarea'),
                            hasInput: !!sib.querySelector('input[type="text"]'),
                        });
                        sib = sib.nextElementSibling;
                    }

                    // Next siblings của parent
                    let psib = hd.parentElement?.nextElementSibling;
                    for (let i = 0; i < 5 && psib; i++) {
                        info.parentNextSiblings.push({
                            tag: psib.tagName,
                            hasTextarea: !!psib.querySelector('textarea'),
                            hasInput: !!psib.querySelector('input[type="text"]'),
                        });
                        psib = psib.nextElementSibling;
                    }

                    out.push(info);
                }
                return out;
            }
        """)

        print("=== Dutch DOM Structure ===")
        for item in result:
            print(f"\n[{item['tag']}] '{item['text']}'")
            print(f"  Parent: <{item['parentTag']} class='{item['parentClass']}'>")
            print(f"  Next siblings of HEADING:")
            for s in item['nextSiblings']:
                print(f"    <{s['tag']}> textarea={s['hasTextarea']} input={s['hasInput']}")
            print(f"  Next siblings of PARENT:")
            for s in item['parentNextSiblings']:
                print(f"    <{s['tag']}> textarea={s['hasTextarea']} input={s['hasInput']}")

asyncio.run(main())
