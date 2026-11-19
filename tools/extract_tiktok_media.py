import asyncio, json, re
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser=await pw.chromium.connect_over_cdp('http://127.0.0.1:9223')
        ctx=browser.contexts[0]; page=await ctx.new_page()
        await page.goto('https://www.tiktok.com/@ririii77',wait_until='domcontentloaded',timeout=60000); await page.wait_for_timeout(5000)
        data=await page.evaluate('''() => ({
          imgs:[...document.images].map(x=>({src:x.currentSrc||x.src,alt:x.alt||'',w:x.naturalWidth,h:x.naturalHeight})).filter(x=>x.src),
          sources:[...document.querySelectorAll('video,video source')].map(x=>x.poster||x.currentSrc||x.src).filter(Boolean),
          links:[...document.querySelectorAll('a[href]')].map(x=>x.href).filter(x=>x.includes('/video/')).slice(0,20)
        })''')
        print(json.dumps(data,ensure_ascii=False,indent=2))
        await page.close()

asyncio.run(main())
