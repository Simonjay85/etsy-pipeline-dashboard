import asyncio, json
from playwright.async_api import async_playwright
URL='https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle'
async def main():
 async with async_playwright() as pw:
  b=await pw.chromium.connect_over_cdp('http://127.0.0.1:9222');c=b.contexts[0];p=await c.new_page();await p.goto(URL,wait_until='domcontentloaded',timeout=60000);await p.wait_for_timeout(4000)
  d=await p.evaluate('''() => ({url:location.href,title:document.title,text:document.body.innerText.slice(0,2000),links:[...document.querySelectorAll('a[href]')].map(a=>({href:a.href,text:(a.innerText||'').trim()})).filter(x=>x.href.includes('collabstr.com/')),buttons:[...document.querySelectorAll('button')].map(x=>(x.innerText||'').trim()).filter(Boolean),pageLinks:[...document.querySelectorAll('a[href]')].map(a=>a.href).filter(x=>/[?&](page|offset|cursor)=/i.test(x)),nav:[...document.querySelectorAll('nav,button,a')].map(x=>({text:(x.innerText||'').trim(),href:x.href||'',aria:x.getAttribute('aria-label')||''})).filter(x=>/next|load more|more creators|page/i.test((x.text+' '+x.href+' '+x.aria).toLowerCase())).slice(-30),scroll:{height:document.body.scrollHeight,innerHeight:innerHeight}})''')
  print(json.dumps({'url':d['url'],'title':d['title'],'text':d['text'],'links':d['links'][:80],'buttons':d['buttons'][:30],'pageLinks':d.get('pageLinks',[]),'nav':d.get('nav',[]),'scroll':d.get('scroll')},ensure_ascii=False))
  await p.close()
asyncio.run(main())
