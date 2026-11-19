from pathlib import Path
from playwright.sync_api import sync_playwright

out = Path('/tmp/current_product08_generated.png')
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp('http://127.0.0.1:9333')
    pages = [x for x in browser.contexts[0].pages if 'chatgpt.com/c/' in x.url]
    if not pages:
        raise SystemExit('no ChatGPT conversation')
    page = pages[-1]
    image = page.locator('img[alt="Generated image"]').last
    if image.count() == 0:
        raise SystemExit('no generated image')
    url = image.get_attribute('src') or image.get_attribute('currentSrc')
    if not url:
        raise SystemExit('no generated URL')
    response = page.context.request.get(url, timeout=180000)
    print('status', response.status, 'bytes', len(response.body()))
    out.write_bytes(response.body())
    print(out)
    browser.close()
