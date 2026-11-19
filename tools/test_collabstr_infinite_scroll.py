import asyncio, json
from playwright.async_api import async_playwright
URL='https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle'
async def main():
 async with async_playwright() as pw:
  b=await pw.chromium.connect_over_cdp('http://127.0.0.1:9222');c=b.contexts[0];p=await c.new_page();await p.goto(URL,wait_until='domcontentloaded',timeout=60000);await p.wait_for_timeout(3000)
  def js(): return '''() => [...new Set([...document.querySelectorAll('a[href*="?p=tiktok"]')].map(a=>a.href))]'''
  before=await p.evaluate(js())
  counts=[]
  for i in range(5):
   await p.evaluate('window.scrollTo(0,document.body.scrollHeight)');await p.wait_for_timeout(2500);counts.append({'step':i+1,'count':len(await p.evaluate(js())),'height':await p.evaluate('document.body.scrollHeight')})
  print(json.dumps({'before':len(before),'after':len(await p.evaluate(js())),'counts':counts,'sample':(await p.evaluate(js()))[:5]},ensure_ascii=False));await p.close()
asyncio.run(main())
