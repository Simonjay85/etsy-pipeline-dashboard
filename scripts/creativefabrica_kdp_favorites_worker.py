#!/usr/bin/env python3
from __future__ import annotations
import csv, fcntl, html, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output'/'creativefabrica'
MASTER=OUT/'kdp_interiors_favorites_all.csv'
STATE=OUT/'kdp_interiors_favorites_state.json'
REPORT=OUT/'kdp_interiors_favorites_progress.md'
LOCK=OUT/'.kdp_interiors_favorites.lock'
CATEGORY='https://www.creativefabrica.com/subscriptions/graphics/kdp-interiors/'
UA='Mozilla/5.0 (compatible; Etsy-CF-Favorites-Research/1.0)'
FIELDS=['product_id','title','favorite_display','favorite_estimate','product_url','http_status','error','first_seen_utc','last_checked_utc']

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

def fetch(url, attempts=4):
    last=None
    for attempt in range(attempts):
        try:
            req=Request(url,headers={'User-Agent':UA})
            with urlopen(req,timeout=60) as response: return response.status,response.read().decode('utf-8','replace')
        except (HTTPError,URLError,TimeoutError) as exc:
            last=exc
            if isinstance(exc,HTTPError) and exc.code not in (408,425,429,500,502,503,504): break
            time.sleep(min(60,2**attempt))
    raise last or RuntimeError('request failed')

def extract_value(text,key,start=0):
    bs=chr(92); marker=key+bs+'":'+bs+'"'; begin=text.find(marker,start)
    if begin<0:return None
    begin+=len(marker); end=text.find(bs+'"',begin)
    return text[begin:end] if end>=0 else None

def parse_category(body):
    bs=chr(92); marker='productId'+bs+'":'+bs+'"'; products=[]; seen=set()
    for match in re.finditer(re.escape(marker),body):
        pos=match.start(); pid=extract_value(body,'productId',pos); purl=extract_value(body,'productUrl',pos); name=extract_value(body,'name',pos) or ''
        if not pid or not purl or pid in seen: continue
        seen.add(pid)
        for old,new in {bs+bs+'u0026':'&',bs+bs+'u2013':'–',bs+bs+'u2014':'—',bs+bs+'u2019':'’',bs+bs+'u0027':"'"}.items(): name=name.replace(old,new)
        products.append({'product_id':pid,'title':html.unescape(name),'product_url':'https://www.creativefabrica.com'+purl})
    return products

def parse_favorite(body):
    match=re.search(r'id="favorite-count"[^>]*>([^<]+)</span>',body,re.I)
    if not match:return '',None
    raw=html.unescape(match.group(1)).strip(); value=raw.upper().replace(',','').replace(' ',''); numeric=None
    for suffix,multiplier in (('KX',1000),('MX',1000000),('X',1)):
        if value.endswith(suffix):
            try:numeric=float(value[:-len(suffix)])*multiplier
            except ValueError:numeric=None
            break
    if numeric is not None and numeric.is_integer(): numeric=int(numeric)
    return raw,numeric

def fetch_product(product):
    checked=now()
    try:
        status,body=fetch(product['product_url']); raw,numeric=parse_favorite(body)
        return {**product,'favorite_display':raw,'favorite_estimate':numeric,'http_status':status,'error':'' if raw else 'favorite-count not found','first_seen_utc':checked,'last_checked_utc':checked}
    except Exception as exc:
        return {**product,'favorite_display':'','favorite_estimate':None,'http_status':None,'error':f'{type(exc).__name__}: {exc}','first_seen_utc':checked,'last_checked_utc':checked}

def load_state():
    if STATE.exists():
        try: state=json.loads(STATE.read_text(encoding='utf-8'))
        except (OSError,json.JSONDecodeError): state={}
    else: state={}
    defaults={'next_category_page':1,'pending_products':[],'processed_ids':[],'completed':False,'total_results_seen':None,'last_run_utc':None,'last_batch_count':0,'runs':0}
    for key,value in defaults.items(): state.setdefault(key,value)
    return state

def save_state(state):
    tmp=STATE.with_suffix('.tmp'); tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8'); tmp.replace(STATE)

def ensure_master():
    OUT.mkdir(parents=True,exist_ok=True)
    if MASTER.exists():return
    page1=OUT/'kdp_interiors_favorites_page1.csv'
    with MASTER.open('w',newline='',encoding='utf-8') as dest:
        writer=csv.DictWriter(dest,fieldnames=FIELDS); writer.writeheader()
        if page1.exists():
            with page1.open(newline='',encoding='utf-8') as src:
                for old in csv.DictReader(src):
                    writer.writerow({'product_id':old.get('product_id',''),'title':old.get('title',''),'favorite_display':old.get('favorite_display',''),'favorite_estimate':old.get('favorite_estimate',''),'product_url':old.get('product_url',''),'http_status':old.get('http_status',''),'error':old.get('error',''),'first_seen_utc':old.get('scraped_at_utc',''),'last_checked_utc':old.get('scraped_at_utc','')})

def load_ids():
    ids=set()
    if MASTER.exists():
        with MASTER.open(newline='',encoding='utf-8') as src:
            for row in csv.DictReader(src):
                if row.get('product_id'):ids.add(row['product_id'])
    return ids

def append_rows(rows):
    existing=load_ids(); new_rows=[row for row in rows if row['product_id'] not in existing]
    if not new_rows:return 0
    with MASTER.open('a',newline='',encoding='utf-8') as dest:
        writer=csv.DictWriter(dest,fieldnames=FIELDS)
        for row in new_rows:writer.writerow(row)
    return len(new_rows)

def write_report(state):
    rows=[]
    if MASTER.exists():
        with MASTER.open(newline='',encoding='utf-8') as src:rows=list(csv.DictReader(src))
    values=[]; errors=0
    for row in rows:
        try:values.append(float(row['favorite_estimate']))
        except (ValueError,TypeError):
            if row.get('error'):errors+=1
    values.sort(); total=int(sum(values)) if values else 0
    median=values[len(values)//2] if values and len(values)%2 else ((values[len(values)//2-1]+values[len(values)//2])/2 if values else 0)
    top=sorted(rows,key=lambda row:float(row['favorite_estimate'] or -1),reverse=True)[:10]
    lines=['# Creative Fabrica — KDP Interiors favorite crawl','',f'- Source: {CATEGORY}',f'- Products collected: {len(rows):,}',f'- Favorite values parsed: {len(values):,}',f'- Rows with errors: {errors:,}',f'- Total displayed favorites (parsed rows): {total:,}',f'- Median: {median:,.1f}',f'- Next category page: {state["next_category_page"]}',f'- Last batch: {state["last_batch_count"]:,}',f'- Runs completed: {state["runs"]:,}',f'- Last run UTC: {state["last_run_utc"] or "n/a"}',f'- Completed: {"yes" if state["completed"] else "no"}','','## Top 10 currently collected','','| # | Product | Favorite | URL |','|---:|---|---:|---|']
    for index,row in enumerate(top,1):
        title=row.get('title','').replace('|','\\|'); lines.append(f'| {index} | {title} | {row.get("favorite_display") or "N/A"} | [Open]({row.get("product_url")}) |')
    REPORT.write_text('\n'.join(lines)+'\n',encoding='utf-8')

def run(daily_limit=10000):
    ensure_master(); state=load_state(); processed=load_ids()|set(state.get('processed_ids',[])); pending=list(state.get('pending_products',[])); added=0
    while added<daily_limit and not state['completed']:
        if not pending:
            page=state['next_category_page']; url=CATEGORY if page==1 else f'{CATEGORY}page/{page}/'; status,body=fetch(url); products=parse_category(body)
            if not products: state['completed']=True; break
            if state['total_results_seen'] is None:
                match=re.search(r'Showing\s+1-36\s+of\s+([\d,]+)\s+results',body)
                if match:state['total_results_seen']=int(match.group(1).replace(',',''))
            pending=[product for product in products if product['product_id'] not in processed]
            if not pending:state['next_category_page']+=1; continue
        take=min(daily_limit-added,len(pending)); batch=pending[:take]
        with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(fetch_product,batch))
        appended=append_rows(results); processed.update(row['product_id'] for row in results); pending=pending[take:]; added+=appended
        if not pending:state['next_category_page']+=1
        state['pending_products']=pending; state['processed_ids']=sorted(processed); state['last_batch_count']=added; state['last_run_utc']=now(); save_state(state)
        if added<daily_limit:time.sleep(0.25)
    state['pending_products']=pending; state['processed_ids']=sorted(processed); state['last_batch_count']=added; state['last_run_utc']=now(); state['runs']+=1; save_state(state); write_report(state)
    print(json.dumps({'added_this_run':added,'collected_total':len(processed),'next_category_page':state['next_category_page'],'completed':state['completed'],'master':str(MASTER),'state':str(STATE),'report':str(REPORT)},ensure_ascii=False))

if __name__=='__main__':
    OUT.mkdir(parents=True,exist_ok=True)
    if (OUT/'vps_handoff.json').exists():
        print('Survey execution moved to VPS; see output/creativefabrica/vps_handoff.json. Local CSV is a retained snapshot.')
        raise SystemExit(0)
    limit=int(sys.argv[1]) if len(sys.argv)>1 else 10000
    with LOCK.open('w') as lockfile:
        try:fcntl.flock(lockfile.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:print('worker already running'); raise SystemExit(0)
        run(limit)
