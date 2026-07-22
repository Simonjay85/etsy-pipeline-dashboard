import asyncio
from playwright.async_api import async_playwright

SECTIONS = [
    "media", "item-details", "category", "how-its-made",
    "about", "pricing-logistics"
]

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        await page.goto("https://www.etsy.com/your/shops/me/listing-editor/create", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        for section in SECTIONS:
            print(f"\n{'='*50}")
            print(f"SECTION: #{section}")
            print('='*50)
            await page.goto(f"https://www.etsy.com/your/shops/me/listing-editor/create#{section}", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            elements = await page.evaluate("""() => {
                const els = document.querySelectorAll('input, textarea, [contenteditable="true"], select');
                return Array.from(els).map(el => ({
                    tag: el.tagName,
                    type: el.type||'',
                    name: el.name||'',
                    id: el.id||'',
                    placeholder: el.placeholder||'',
                    aria: el.getAttribute('aria-label')||'',
                    testid: el.getAttribute('data-testid')||'',
                    role: el.getAttribute('role')||'',
                    visible: el.offsetParent !== null
                })).filter(e => e.visible);
            }""")

            for el in elements:
                print(f"  {el['tag']} | name='{el['name']}' | id='{el['id']}' | placeholder='{el['placeholder']}' | aria='{el['aria']}' | testid='{el['testid']}'")

            # Radio/checkbox buttons
            radios = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('[role="radio"], [role="checkbox"], button[aria-pressed]'))
                    .filter(e => e.offsetParent !== null)
                    .map(e => ({
                        text: e.textContent.trim().substring(0,40),
                        role: e.getAttribute('role'),
                        pressed: e.getAttribute('aria-pressed'),
                        checked: e.getAttribute('aria-checked')
                    }));
            }""")
            for r in radios:
                if r['text']:
                    print(f"  RADIO/BTN: '{r['text']}' | pressed={r['pressed']} | checked={r['checked']}")

asyncio.run(main())
