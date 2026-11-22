from playwright.sync_api import sync_playwright
with sync_playwright() as p:
 b=p.chromium.connect_over_cdp('http://127.0.0.1:9333');pg=[x for x in b.contexts[0].pages if 'chatgpt.com/c/' in x.url][0]
 print(pg.evaluate("""() => [...document.querySelectorAll('img')].map((x,i)=>({i,alt:x.alt,src:(x.currentSrc||x.src||'').slice(0,240),cls:x.className,box:(()=>{const r=x.getBoundingClientRect();return [r.x,r.y,r.width,r.height]})()})).filter(x=>x.src.includes('/backend-api/estuary/content')||x.alt)"""))
 print('buttons',pg.evaluate("() => [...document.querySelectorAll('button')].map((x,i)=>({i,aria:x.getAttribute('aria-label'),text:(x.innerText||'').slice(0,80)})).filter(x=>x.aria||x.text).slice(-80)"))
 b.close()
