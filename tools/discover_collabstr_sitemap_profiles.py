import asyncio, json, re
from pathlib import Path
from playwright.async_api import async_playwright
OUT=Path('/Users/aaronnguyen/Developer/Etsy/output/collabstr_sitemap_profile_urls.json')
async def main():
 async with async_playwright() as pw:
  b=await pw.chromium.connect_over_cdp('http://127.0.0.1:9222');c=b.contexts[0];p=await c.new_page();urls=set()
  for i in [1,2,3]:
   await p.goto(f'https://collabstr.com/sitemap.xml/{i}',wait_until='domcontentloaded',timeout=60000);await p.wait_for_timeout(1200)
   vals=await p.locator('loc').all_text_contents()
   if not vals:
    vals=re.findall(r'https://collabstr\.com/[A-Za-z0-9._~-]+',await p.locator('body').inner_text())
   for u in vals:
    u=u.strip()
    if re.fullmatch(r'https://collabstr\.com/[A-Za-z0-9._~-]+',u): urls.add(u)
  result={'source_sitemaps':[f'https://collabstr.com/sitemap.xml/{i}' for i in [1,2,3]],'count':len(urls),'profile_urls':sorted(urls)}
  OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print({'count':len(urls),'output':str(OUT),'sample':sorted(urls)[:5]});await p.close()
asyncio.run(main())
