from playwright.sync_api import sync_playwright
with sync_playwright() as p:
 b=p.chromium.connect_over_cdp('http://127.0.0.1:9333')
 for i,pg in enumerate(b.contexts[0].pages):
  print('PAGE',i,pg.url)
  try:
   print('title',pg.title(),'generated',pg.locator('img[alt="Generated image"]').count(),'estuary',pg.evaluate("() => [...document.images].filter(x=>(x.currentSrc||x.src||'').includes('/backend-api/estuary/content')).length"))
   print('alts',pg.evaluate("() => [...document.images].map(x=>x.alt).filter(Boolean).slice(-30)"))
  except Exception as e:print('ERR',e)
 b.close()
