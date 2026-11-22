from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path('/Users/aaronnguyen/Developer/Etsy/shops/BubbleByJulie/product-08/images');files=sorted(root.glob('*.png'))
with sync_playwright() as p:
 b=p.chromium.connect_over_cdp('http://127.0.0.1:9333');c=b.contexts[0];pg=c.new_page();pg.goto('https://chatgpt.com/',wait_until='domcontentloaded',timeout=120000);pg.wait_for_selector('#prompt-textarea',state='visible',timeout=120000);pg.locator('button[data-testid="composer-plus-btn"]').click();pg.get_by_text('Create image',exact=True).last.click();pg.locator('#upload-files').set_input_files([str(x) for x in files]);pg.wait_for_timeout(10000)
 print('file_count',pg.locator('#upload-files').evaluate('n=>n.files.length'))
 print('imgs',pg.evaluate("() => [...document.images].map(x=>({alt:x.alt,src:(x.currentSrc||x.src||'').slice(0,180)})).filter(x=>x.alt||x.src.includes('estuary')).slice(-40)"))
 print('estuary_count',pg.evaluate("() => [...document.images].filter(x=>(x.currentSrc||x.src||'').includes('/backend-api/estuary/content')).length"))
 print('remove_controls',pg.locator('[aria-label*="Remove file" i]').count())
 pg.close();b.close()
