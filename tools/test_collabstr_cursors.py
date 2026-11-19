import asyncio, json
from playwright.async_api import async_playwright
BASE='https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle'
async def main():
 async with async_playwright() as pw:
  b=await pw.chromium.connect_over_cdp('http://127.0.0.1:9222');c=b.contexts[0];p=await c.new_page();out=[]
  for cur in range(0,11):
   u=BASE+('&cursor='+str(cur) if cur else '')
   await p.goto(u,wait_until='domcontentloaded',timeout=60000);await p.wait_for_timeout(1800)
   links=await p.evaluate("() => [...new Set([...document.querySelectorAll('a[href*=\\\"?p=tiktok\\\"]')].map(a=>a.href))]")
   out.append({'cursor':cur,'first':links[:2],'count':len(links),'last':links[-2:]})
  print(json.dumps(out,ensure_ascii=False));await p.close()
asyncio.run(main())
