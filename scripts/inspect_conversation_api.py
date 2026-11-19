import re
from playwright.sync_api import sync_playwright
cid='6aa517fb-71d4-83ec-bb39-a67caaa1f391'
with sync_playwright() as p:
 b=p.chromium.connect_over_cdp('http://127.0.0.1:9333');pages=[x for x in b.contexts[0].pages if 'chatgpt.com/c/' in x.url];pg=pages[-1]
 r=pg.context.request.get(f'https://chatgpt.com/backend-api/conversation/{cid}',timeout=120000)
 print('status',r.status,'bytes',len(r.body()))
 if r.status==200:
  data=r.json();print('top_keys',sorted(data.keys()) if isinstance(data,dict) else type(data).__name__)
  hits=[]
  def walk(x,path=''):
   if isinstance(x,dict):
    for k,v in x.items():walk(v,f'{path}.{k}')
   elif isinstance(x,list):
    for i,v in enumerate(x):walk(v,f'{path}[{i}]')
   elif isinstance(x,str) and '/backend-api/estuary/content' in x:
    m=re.search(r'[?&]id=([^&]+)',x);hits.append((path,m.group(1) if m else 'noid'))
  walk(data);print('estuary_strings',len(hits),'unique_ids',len({x[1] for x in hits}));print('last_paths',hits[-30:])
 b.close()
