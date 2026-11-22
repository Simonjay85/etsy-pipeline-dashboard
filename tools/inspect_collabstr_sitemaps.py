import asyncio, json
from playwright.async_api import async_playwright
async def main():
 async with async_playwright() as pw:
  b=await pw.chromium.connect_over_cdp('http://127.0.0.1:9222');c=b.contexts[0];p=await c.new_page();out=[]
  for i in range(5):
   try:
    await p.goto(f'https://collabstr.com/sitemap.xml/{i}',wait_until='domcontentloaded',timeout=60000);await p.wait_for_timeout(1500)
    out.append({'i':i,'url':p.url,'text':(await p.locator('body').inner_text())[:500],'title':await p.title()})
   except Exception as e: out.append({'i':i,'error':type(e).__name__+':'+str(e)[:200]})
  print(json.dumps(out,ensure_ascii=False));await p.close()
asyncio.run(main())
