import asyncio, json
from urllib.parse import urlencode
from playwright.async_api import async_playwright
BASE='https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle'
async def main():
 async with async_playwright() as pw:
  b=await pw.chromium.connect_over_cdp('http://127.0.0.1:9222');c=b.contexts[0];p=await c.new_page();out=[]
  for suffix in ['&page=2','&offset=24','&cursor=2','&page=3']:
   await p.goto(BASE+suffix,wait_until='domcontentloaded',timeout=60000);await p.wait_for_timeout(2500)
   links=await p.evaluate("() => [...new Set([...document.querySelectorAll('a[href*=\\\"?p=tiktok\\\"]')].map(a=>a.href))]")
   out.append({'suffix':suffix,'url':p.url,'title':await p.title(),'count':len(links),'links':links[:3],'text':(await p.locator('body').inner_text())[:250]})
  print(json.dumps(out,ensure_ascii=False));await p.close()
asyncio.run(main())
