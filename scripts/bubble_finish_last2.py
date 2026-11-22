import json,re,time,hashlib,shutil,urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright
from websockets.sync.client import connect
ROOT=Path('/Users/aaronnguyen/Developer/Etsy/shops/BubbleByJulie');STATE=ROOT/'image_regen_state.json'
def cdp_set(url,files,prompt):
 ts=json.load(urllib.request.urlopen('http://127.0.0.1:9333/json/list'));t=next(x for x in ts if x.get('url')==url and x.get('type')=='page');ws=connect(t['webSocketDebuggerUrl'],max_size=50*1024*1024);i=0
 def c(m,p={}):
  nonlocal i;i+=1;ws.send(json.dumps({'id':i,'method':m,'params':p}))
  while 1:
   r=json.loads(ws.recv())
   if r.get('id')==i:return r
 root=c('DOM.getDocument',{'depth':0})['result']['root']['nodeId'];nid=c('DOM.querySelector',{'nodeId':root,'selector':'#upload-files'})['result']['nodeId'];c('DOM.setFileInputFiles',{'nodeId':nid,'files':[str(x) for x in files]});c('Runtime.evaluate',{'expression':'document.querySelector("#prompt-textarea").focus()'});c('Input.insertText',{'text':prompt});ws.close()
def urls(pg):
 ss=pg.evaluate("() => [...document.querySelectorAll('img')].map(x=>x.currentSrc||x.src||'').filter(x=>x.includes('/backend-api/estuary/content'))");o=[];s=set()
 for u in ss:
  m=re.search(r'[?&]id=([^&]+)',u)
  if m and m.group(1) not in s:s.add(m.group(1));o.append(u)
 return o
with sync_playwright() as p:
 b=p.chromium.connect_over_cdp('http://127.0.0.1:9333');pg=[x for x in b.contexts[0].pages if 'chatgpt.com' in x.url][-1];text=pg.evaluate('document.body.innerText');a=text.find('CREATE A COMPLETE ETSY LISTING IMAGE SET FROM THE UPLOADED JSON FILE AND PRODUCT PHOTOS.');bb=text.find('\nShow more\nWorked for',a);prompt=text[a:bb].strip()+"\n\nFINAL OVERRIDE: No customer-facing text anywhere. No title, product name, design code, dimensions, material, color, feature text, category labels, CTA, logo, watermark, or typography. Image-only square Etsy listing photos. Return exactly 10 separate image files."
 state=json.loads(STATE.read_text());
 for n in [174,175]:
  name=f'product-{n:02d}'
  if name in state['done'] and n != 7:continue
  src=ROOT/name/'source_catalog';files=[src/'source.json']+sorted((src/'thamkhao').glob('*'));url=pg.url;before=len(urls(pg));print('START',name,flush=True);cdp_set(url,files,prompt);pg.wait_for_timeout(3000);print('files',pg.evaluate("() => document.querySelector('#upload-files').files.length"),flush=True);pg.locator('button[aria-label="Send prompt"]').last.click();print('SENT',name,flush=True);start=time.time();seen=False
  while time.time()-start<900:
   stop=pg.locator('button[aria-label*="Stop" i]').count();now=len(urls(pg));print('poll',int(time.time()-start),stop,now,flush=True)
   if stop:seen=True
   if seen and stop==0:time.sleep(8);break
   time.sleep(30)
  if pg.locator('button[aria-label*="Stop" i]').count():raise SystemExit(f'timeout {name}')
  batch=urls(pg)[-10:]
  if len(batch)!=10:raise SystemExit(f'bad batch {name} {len(batch)}')
  tmp=ROOT/name/'images_regen_tmp';shutil.rmtree(tmp,ignore_errors=True);tmp.mkdir()
  for i,u in enumerate(batch,1):
   r=pg.context.request.get(u,timeout=180000)
   if r.status!=200 or len(r.body())<10000:raise SystemExit(f'download {name} {i}')
   (tmp/f'etsy_{i:02d}.png').write_bytes(r.body())
  if len({hashlib.sha256(x.read_bytes()).hexdigest() for x in tmp.glob('*.png')})!=10:raise SystemExit(f'duplicate {name}')
  dest=ROOT/name/'images';dest.mkdir(exist_ok=True)
  for x in dest.glob('*'):x.unlink()
  for x in tmp.glob('*.png'):shutil.move(str(x),dest/x.name)
  tmp.rmdir();state['done'].append(name);STATE.write_text(json.dumps(state,indent=2));print('DONE',name,flush=True)
 b.close()