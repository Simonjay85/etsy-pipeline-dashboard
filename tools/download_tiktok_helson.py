import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

OUT=Path('/Users/aaronnguyen/Developer/Etsy/collabstr_tiktok_lifestyle_kol_profiles/assets/tiktok/helson')
OUT.mkdir(parents=True,exist_ok=True)

async def main():
    async with async_playwright() as pw:
        browser=await pw.chromium.connect_over_cdp('http://127.0.0.1:9223')
        ctx=browser.contexts[0]; page=await ctx.new_page()
        await page.goto('https://www.tiktok.com/@ririii77',wait_until='domcontentloaded',timeout=60000); await page.wait_for_timeout(5000)
        data=await page.evaluate('''() => ({
          images:[...document.images].map(x=>x.currentSrc||x.src).filter(x=>x && !x.startsWith('data:') && /tplv-(photomode|tiktokx)/i.test(x)),
          text:document.body.innerText
        })''')
        images=[]
        for url in data['images']:
            if url not in images: images.append(url)
        images=images[1:4] if len(images)>=4 else images[:3]
        saved=[]
        for i,url in enumerate(images,1):
            resp=await ctx.request.get(url,timeout=60000)
            body=await resp.body()
            ext='avif' if 'avif' in (resp.headers.get('content-type','') or '') or '.avif' in url else 'jpg'
            path=OUT/f'{i:02d}.{ext}'; path.write_bytes(body)
            saved.append({'url':url,'path':str(path),'bytes':len(body),'content_type':resp.headers.get('content-type','')})
        (OUT/'source.json').write_text(json.dumps({'source':'https://www.tiktok.com/@ririii77','images':saved},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'saved':saved,'text':data['text'][:300]},ensure_ascii=False))
        await page.close()

asyncio.run(main())
