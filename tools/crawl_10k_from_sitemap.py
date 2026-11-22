#!/usr/bin/env python3
import asyncio, json, re, sys, time, os
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT=Path(os.environ.get('COLLABSTR_ROOT','/Users/aaronnguyen/Developer/Etsy'))
SITEMAP=ROOT/'output/collabstr_sitemap_profile_urls.json'
EXISTING=[ROOT/'output/collabstr_live_crawl_full.jsonl',ROOT/'output/collabstr_live_crawl_checkpoint.jsonl']
OUT=ROOT/'output/collabstr_10k_sitemap_crawl.jsonl'
PROGRESS=ROOT/'output/collabstr_10k_sitemap_progress.json'
CDP=os.environ.get('COLLABSTR_CDP','http://127.0.0.1:9222'); TARGET=10000

EXTRACT=r'''() => { const clean=v=>(v||'').replace(/\s+/g,' ').trim(); const scripts=[...document.querySelectorAll('script[type="application/ld+json"]')].map(s=>{try{return JSON.parse(s.textContent)}catch(_){return null}}).filter(Boolean); const flat=x=>Array.isArray(x)?x.flatMap(flat):(x&&typeof x==='object'?[x,...Object.values(x).flatMap(flat)]:[]); const ld=flat(scripts); const person=ld.find(x=>x['@type']==='Person')||{}; const agg=ld.find(x=>x['@type']==='AggregateRating')||person.aggregateRating||{}; const imgs=[...new Set([...document.images].map(x=>x.currentSrc||x.src).filter(x=>x&&/cloudfront\.net\/sellers\//i.test(x)&&!/websiteImages/i.test(x)))].slice(0,12); const vids=[...new Set([...document.querySelectorAll('video,video source')].map(x=>x.currentSrc||x.src).filter(x=>x&&/cloudfront\.net\/sellers\//i.test(x)))].slice(0,24); const links=[...new Set([...document.querySelectorAll('a[href]')].map(x=>x.href).filter(x=>/instagram\.com|tiktok\.com|youtube\.com/i.test(x)))]; const body=document.body?document.body.innerText:''; return {title:document.title,heading:clean(document.querySelector('h1')?.innerText||document.querySelector('h2')?.innerText),name:clean(person.name||''),bio:clean(person.description||''),location:clean(person.homeLocation?.name||person.address?.addressLocality||''),social:links,rating:agg,gallery:imgs,portfolio:vids,visible_text:body.slice(0,50000),is_tiktok:/tiktok/i.test(document.title+' '+body),is_lifestyle:/lifestyle/i.test(document.title+' '+body)}; }'''

def base_key(u): return urlparse(u).netloc+urlparse(u).path.rstrip('/').lower()
def load_existing():
 out=set()
 for p in EXISTING:
  if not p.exists(): continue
  for line in p.read_text(encoding='utf-8').splitlines():
   try:
    d=json.loads(line)
    if d.get('status')=='ok' and d.get('source_url'): out.add(base_key(d['source_url']))
   except Exception: pass
 return out

def load_done_new():
 out=set()
 if OUT.exists():
  for line in OUT.read_text(encoding='utf-8').splitlines():
   try:
    d=json.loads(line)
    if d.get('source_url'): out.add(base_key(d['source_url']))
   except Exception: pass
 return out

def append(d):
 with OUT.open('a',encoding='utf-8') as f:f.write(json.dumps(d,ensure_ascii=False)+'\n')

def save(current,processed,ok,errors,blocked):
 PROGRESS.write_text(json.dumps({'target':TARGET,'seed_profiles':current-processed,'new_processed':processed,'current_total':current,'new_ok':ok,'errors':errors,'blocked':blocked,'remaining':max(0,TARGET-current),'output':str(OUT),'updated_at':time.strftime('%Y-%m-%dT%H:%M:%S%z')},ensure_ascii=False,indent=2),encoding='utf-8')

async def main():
 limit=int(sys.argv[1]) if len(sys.argv)>1 else 0
 data=json.loads(SITEMAP.read_text(encoding='utf-8')); urls=data['profile_urls']; seed=load_existing(); done=load_done_new(); all_done=seed|done
 todo=[u for u in urls if base_key(u) not in all_done]
 if limit: todo=todo[:limit]
 current=len(seed|done); processed=ok=errors=blocked=0
 print(json.dumps({'sitemap_urls':len(urls),'seed_profiles':len(seed),'new_done':len(done),'current_total':current,'to_process':len(todo),'target':TARGET}),flush=True)
 async with async_playwright() as pw:
  browser=await pw.chromium.connect_over_cdp(CDP);ctx=browser.contexts[0];page=await ctx.new_page()
  for idx,u in enumerate(todo,1):
   if current>=TARGET: break
   url=u+'?p=tiktok'
   try:
    await page.goto(url,wait_until='domcontentloaded',timeout=60000);await page.wait_for_timeout(2200)
    title=await page.title();body=(await page.locator('body').inner_text(timeout=15000))[:1200]
    if any(x in (title+' '+body).lower() for x in ['just a moment','verify you are human','checking your browser']):
     append({'source_url':u,'crawl_url':url,'status':'blocked','error':'browser_challenge'});blocked+=1;print(json.dumps({'n':idx,'status':'blocked','url':u}),flush=True);save(current,processed,ok,errors,blocked);break
    live=await page.evaluate(EXTRACT);append({'source_url':u,'crawl_url':url,'status':'ok','live':live});ok+=1;processed+=1;current+=1
    print(json.dumps({'n':idx,'status':'ok','url':u,'tiktok':live.get('is_tiktok'),'lifestyle':live.get('is_lifestyle'),'images':len(live.get('gallery') or []),'videos':len(live.get('portfolio') or [])}),flush=True)
   except Exception as e:
    if type(e).__name__=='TargetClosedError':
     page=await ctx.new_page();print(json.dumps({'n':idx,'status':'page_reopened','url':u}),flush=True);continue
    append({'source_url':u,'crawl_url':url,'status':'error','error':type(e).__name__+':'+str(e)[:300]});errors+=1;processed+=1;print(json.dumps({'n':idx,'status':'error','url':u,'error':type(e).__name__}),flush=True)
   if idx%10==0:save(current,processed,ok,errors,blocked)
   try: await page.wait_for_timeout(3000)
   except Exception:
    page=await ctx.new_page();continue
   if idx%100==0:
    try: await page.wait_for_timeout(30000)
    except Exception: page=await ctx.new_page()
  await page.close()
 save(current,processed,ok,errors,blocked);print(json.dumps({'finished':current>=TARGET,'current_total':current,'new_ok':ok,'errors':errors,'blocked':blocked,'output':str(OUT)}))

asyncio.run(main())
