import asyncio
import json
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parent
TARGET='4428935365'
async def main():
    async with async_playwright() as pw:
        browser=await pw.chromium.connect_over_cdp('http://127.0.0.1:41822',timeout=10000)
        page=await browser.contexts[0].new_page()
        await page.goto(f'https://www.etsy.com/your/shops/me/listing-editor/edit/{TARGET}',wait_until='domcontentloaded',timeout=90000)
        await page.wait_for_selector('#listing-title-input',timeout=60000); await page.wait_for_timeout(3000)
        result={'listing_id':TARGET,'url':page.url,'title':await page.locator('#listing-title-input').input_value(),'page_title':await page.title(),'captured_at':datetime.now().isoformat(timespec='seconds')}
        path=ROOT/f'scratch/temply_editor_readonly_{TARGET}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'snapshot':str(path),'listing_id':TARGET,'title':result['title']},ensure_ascii=False))
        await page.close()
asyncio.run(main())
