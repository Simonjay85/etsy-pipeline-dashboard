import re,time,json,hashlib,shutil
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path('/Users/aaronnguyen/Developer/Etsy/shops/BubbleByJulie');prod=ROOT/'product-174'
def urls(pg):
 ss=pg.evaluate("() => [...document.querySelectorAll('img')].map(x=>x.currentSrc||x.src||'').filter(x=>x.includes('/backend-api/estuary/content'))");o=[];s=set()
 for u in ss:
  m=re.search(r'[?&]id=([^&]+)',u)
  if m and m.group(1) not in s:s.add(m.group(1));o.append(u)
 return o
with sync_playwright() as p:
 b=p.chromium.connect_over_cdp('http://127.0.0.1:9333');pg=[x for x in b.contexts[0].pages if 'chatgpt.com' in x.url][-1];start=time.time();seen=False
 while time.time()-start<900:
  stop=pg.locator('button[aria-label*="Stop" i]').count();n=len(urls(pg));print('poll',int(time.time()-start),stop,n,flush=True)
  if stop:seen=True
  if seen and stop==0:
   time.sleep(8);break
  time.sleep(30)
 if pg.locator('button[aria-label*="Stop" i]').count():raise SystemExit('timeout 15m')
 us=urls(pg)[-10:]
 if len(us)!=10:raise SystemExit(f'bad batch {len(us)}')
 tmp=prod/'images_regen_tmp';shutil.rmtree(tmp,ignore_errors=True);tmp.mkdir()
 for i,u in enumerate(us,1):
  r=pg.context.request.get(u,timeout=180000)
  if r.status!=200 or len(r.body())<10000:raise SystemExit('bad download')
  (tmp/f'etsy_{i:02d}.png').write_bytes(r.body())
 if len({hashlib.sha256(x.read_bytes()).hexdigest() for x in tmp.glob('*.png')})!=10:raise SystemExit('duplicates')
 dest=prod/'images';dest.mkdir(exist_ok=True)
 for x in dest.glob('*'):x.unlink()
 for x in tmp.glob('*.png'):shutil.move(str(x),dest/x.name)
 tmp.rmdir();state=json.loads((ROOT/'image_regen_state.json').read_text());
 if 'product-174' not in state['done']:state['done'].append('product-174')
 (ROOT/'image_regen_state.json').write_text(json.dumps(state,indent=2));print('DONE product-174');b.close()