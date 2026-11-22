import asyncio, json, re
from playwright.async_api import async_playwright

URLS=[
'https://www.tiktok.com/@ririii77/video/7686140672360926482',
'https://www.tiktok.com/@ririii77/video/7686123327294835975',
'https://www.tiktok.com/@ririii77/video/7680925677419416840',
'https://www.tiktok.com/@ririii77/video/7680922657210125586',
'https://www.tiktok.com/@ririii77/video/7679028862960880904',
'https://www.tiktok.com/@ririii77/video/7677914819013446930'
]
async def main():
 async with async_playwright() as pw:
  browser=await pw.chromium.connect_over_cdp('http://127.0.0.1:9223');ctx=browser.contexts[0];page=await ctx.new_page();out=[]
  for u in URLS:
   try:
    await page.goto(u,wait_until='domcontentloaded',timeout=60000);await page.wait_for_timeout(4000)
    d=await page.evaluate('''() => ({title:document.title,text:document.body.innerText.slice(0,5000),meta:[...document.querySelectorAll('meta')].map(x=>({p:x.getAttribute('property'),n:x.getAttribute('name'),c:x.getAttribute('content')})).filter(x=>x.c)})''')
    out.append({'url':u,'status':'ok','title':d['title'],'text':d['text'],'meta':d['meta']})
   except Exception as e: out.append({'url':u,'status':'error','error':type(e).__name__+':'+str(e)[:300]})
  print(json.dumps(out,ensure_ascii=False))
  await page.close()
asyncio.run(main())
