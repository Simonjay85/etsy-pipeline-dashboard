#!/usr/bin/env python3
import asyncio, csv, json, re, time, hashlib
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT=Path('/Users/aaronnguyen/Developer/Etsy')
INPUTS=[ROOT/'output/collabstr_live_crawl_full.jsonl',ROOT/'output/collabstr_live_crawl_checkpoint.jsonl']
OUT=ROOT/'output/tiktok_profile_media.jsonl'
PROGRESS=ROOT/'output/tiktok_profile_media_progress.json'
CDP='http://127.0.0.1:9223'

def slug(value):
    return re.sub(r'[^a-z0-9]+','-',value.lower()).strip('-') or hashlib.sha1(value.encode()).hexdigest()[:10]

def records():
    out={}
    for path in INPUTS:
        if not path.exists(): continue
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                d=json.loads(line)
                if d.get('status')=='ok' and d.get('source_url'): out[d['source_url']]=d
            except Exception: pass
    return list(out.values())

def done_urls():
    done=set()
    if OUT.exists():
        for line in OUT.read_text(encoding='utf-8').splitlines():
            try:
                d=json.loads(line)
                if d.get('source_url'): done.add(d['source_url'])
            except Exception: pass
    return done

def append(d):
    with OUT.open('a',encoding='utf-8') as f: f.write(json.dumps(d,ensure_ascii=False)+'\n')

def save(total,processed,ok,errors,no_link):
    PROGRESS.write_text(json.dumps({'source_profiles':total,'processed':processed,'ok':ok,'errors':errors,'no_tiktok_link':no_link,'remaining':total-processed,'output':str(OUT),'updated_at':time.strftime('%Y-%m-%dT%H:%M:%S%z')},ensure_ascii=False,indent=2),encoding='utf-8')

async def main():
    rows=records(); done=done_urls(); todo=[r for r in rows if r['source_url'] not in done]
    ok=errors=no_link=0
    print(json.dumps({'source_profiles':len(rows),'already_done':len(done),'to_process':len(todo)}),flush=True)
    async with async_playwright() as pw:
        browser=await pw.chromium.connect_over_cdp(CDP); ctx=browser.contexts[0]; page=await ctx.new_page()
        for idx,r in enumerate(todo,1):
            live=r.get('live') or {}; socials=live.get('social') or []
            tiktok=next((x for x in socials if 'tiktok.com' in x.lower() and '@collabstr.com' not in x.lower() and '/@' in x.lower()),None)
            if not tiktok:
                title=(live.get('title') or '')
                m=re.search(r'\\(@([A-Za-z0-9._-]{2,}))',title)
                tiktok=f'https://www.tiktok.com/@{m.group(1)}' if m else None
            if not tiktok:
                append({'source_url':r['source_url'],'username':r.get('username',''),'status':'no_tiktok_link'}); no_link+=1; continue
            try:
                await page.goto(tiktok,wait_until='domcontentloaded',timeout=60000); await page.wait_for_timeout(3500)
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)'); await page.wait_for_timeout(1200)
                await page.evaluate('window.scrollTo(0, 0)'); await page.wait_for_timeout(500)
                body=(await page.locator('body').inner_text(timeout=15000))[:6000]
                if any(x in body.lower() for x in ['verify you are human','just a moment','checking your browser']):
                    append({'source_url':r['source_url'],'username':r.get('username',''),'tiktok_url':tiktok,'status':'blocked'}); errors+=1; continue
                data=await page.evaluate('''() => { const imgs=[...document.images].map(x=>x.currentSrc||x.src).filter(x=>x&&!x.startsWith('data:')&&!/\\/avt-\\//i.test(x)&&/tplv-(photomode|tiktokx)/i.test(x)); return {images:[...new Set(imgs)],text:document.body.innerText}; }''')
                media=[]
                for u in data.get('images',[]):
                    if u not in media: media.append(u)
                media=media[:3]
                if len(media) < 3: raise RuntimeError('fewer_than_3_tiktok_images')
                count_match=re.search(r'([\d.,]+\s*[KMB]?)\s*Followers',data.get('text',''),re.I)
                followers=count_match.group(1).replace(' ','') if count_match else None
                asset_dir=ROOT/'collabstr_tiktok_lifestyle_kol_profiles/assets/tiktok'/slug(r.get('username') or urlparse(r['source_url']).path.strip('/'))
                asset_dir.mkdir(parents=True,exist_ok=True); local=[]
                for n,u in enumerate(media,1):
                    resp=await ctx.request.get(u,timeout=60000); body_bytes=await resp.body(); ctype=resp.headers.get('content-type','')
                    ext='avif' if 'avif' in ctype or '.avif' in u else 'jpg'
                    path=asset_dir/f'{n:02d}.{ext}'; path.write_bytes(body_bytes); local.append(str(path))
                append({'source_url':r['source_url'],'username':r.get('username',''),'tiktok_url':tiktok,'status':'ok','followers':followers,'image_urls':media,'local_images':local})
                ok+=1; print(json.dumps({'n':idx,'status':'ok','url':tiktok,'followers':followers,'images':len(media)}),flush=True)
            except Exception as e:
                errors+=1; append({'source_url':r['source_url'],'username':r.get('username',''),'tiktok_url':tiktok,'status':'error','error':type(e).__name__+': '+str(e)[:300]}); print(json.dumps({'n':idx,'status':'error','url':tiktok,'error':type(e).__name__}),flush=True)
            if idx%10==0: save(len(rows),len(done)+idx,ok,errors,no_link)
            await page.wait_for_timeout(900)
        await page.close()
    save(len(rows),len(done)+len(todo),ok,errors,no_link); print(json.dumps({'finished':True,'ok':ok,'errors':errors,'no_tiktok_link':no_link,'output':str(OUT)}))

asyncio.run(main())
