import asyncio, json
from playwright.async_api import async_playwright
URL='https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle'
async def main():
 async with async_playwright() as pw:
  b=await pw.chromium.connect_over_cdp('http://127.0.0.1:9222');c=b.contexts[0];p=await c.new_page();req=[]
  p.on('request',lambda r: req.append({'url':r.url,'method':r.method,'resource':r.resource_type}))
  await p.goto(URL,wait_until='networkidle',timeout=90000);await p.wait_for_timeout(1000)
  print(json.dumps([x for x in req if any(k in x['url'].lower() for k in ['api','influenc','creator','graphql'])][-100:],ensure_ascii=False))
  await p.close()
asyncio.run(main())
