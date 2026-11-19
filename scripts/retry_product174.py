import json,urllib.request,time
from pathlib import Path
from playwright.sync_api import sync_playwright
from websockets.sync.client import connect
ROOT=Path('/Users/aaronnguyen/Developer/Etsy/shops/BubbleByJulie');src=ROOT/'product-174/source_catalog';files=[src/'source.json']+sorted((src/'thamkhao').glob('*'))
with sync_playwright() as p:
 b=p.chromium.connect_over_cdp('http://127.0.0.1:9333');pages=[x for x in b.contexts[0].pages if 'chatgpt.com' in x.url];prompt=None
 for pg in pages:
  try:
   text=pg.evaluate('document.body.innerText');a=text.find('CREATE A COMPLETE ETSY LISTING IMAGE SET FROM THE UPLOADED JSON FILE AND PRODUCT PHOTOS.');bb=text.find('\nShow more\nWorked for',a)
   if a>=0 and bb>0:prompt=text[a:bb].strip();break
  except:pass
 if not prompt:raise SystemExit('prompt missing')
 prompt+='\n\nFINAL OVERRIDE: No customer-facing text anywhere. No title, product name, design code, dimensions, material, color, feature text, category labels, CTA, logo, watermark, or typography. Image-only square Etsy listing photos. Return exactly 10 separate image files.'
 for pg in pages:
  try:
   if 'chatgpt.com/c/' in pg.url:
    st=pg.locator('button[aria-label*="Stop" i]').last
    if st.count() and st.is_visible():st.click()
    pg.close()
  except:pass
 pg=b.contexts[0].new_page();pg.goto('https://chatgpt.com/',wait_until='domcontentloaded',timeout=120000);pg.wait_for_timeout(20000);pg.locator('button[data-testid="composer-plus-btn"]').click();pg.get_by_text('Create image',exact=True).click();pg.wait_for_timeout(2000);url=pg.url;b.close()
ts=json.load(urllib.request.urlopen('http://127.0.0.1:9333/json/list'));t=next(x for x in ts if x.get('url')==url and x.get('type')=='page');ws=connect(t['webSocketDebuggerUrl'],max_size=50*1024*1024);i=0
def c(m,p={}):
 global i;i+=1;ws.send(json.dumps({'id':i,'method':m,'params':p}))
 while 1:
  r=json.loads(ws.recv())
  if r.get('id')==i:return r
root=c('DOM.getDocument',{'depth':0})['result']['root']['nodeId'];nid=c('DOM.querySelector',{'nodeId':root,'selector':'#upload-files'})['result']['nodeId'];c('DOM.setFileInputFiles',{'nodeId':nid,'files':[str(x) for x in files]});c('Runtime.evaluate',{'expression':'document.querySelector("#prompt-textarea").focus()'});c('Input.insertText',{'text':prompt});ws.close()
with sync_playwright() as p:
 b=p.chromium.connect_over_cdp('http://127.0.0.1:9333');pg=[x for x in b.contexts[0].pages if x.url==url][0];pg.wait_for_timeout(3000);print('files',pg.evaluate("() => document.querySelector('#upload-files').files.length"),'editor',len(pg.locator('#prompt-textarea').inner_text()));pg.locator('button[aria-label="Send prompt"]').last.click();print('SENT product-174 retry');b.close()