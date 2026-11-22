import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright
URL='https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle'
OUT=Path('/Users/aaronnguyen/Developer/Etsy/output/collabstr_lifestyle_visible_urls.json')
async def main():
 async with async_playwright() as pw:
  b=await pw.chromium.connect_over_cdp('http://127.0.0.1:9222');c=b.contexts[0];p=await c.new_page();await p.goto(URL,wait_until='domcontentloaded',timeout=60000);await p.wait_for_timeout(3000)
  links=await p.evaluate("() => [...new Set([...document.querySelectorAll('a[href*=\\\"?p=tiktok\\\"]')].map(a=>a.href))]")
  OUT.write_text(json.dumps({'source_url':URL,'visible_profile_urls':links,'count':len(links)},ensure_ascii=False,indent=2),encoding='utf-8')
  print({'count':len(links),'output':str(OUT),'first':links[:3],'last':links[-3:]});await p.close()
asyncio.run(main())
