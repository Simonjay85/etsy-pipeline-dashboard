#!/usr/bin/env python3
from pathlib import Path
from collections import Counter
import hashlib, json
import openpyxl
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
SHOP=ROOT/'shops/shop3'
BASE=ROOT/'output/diagnostics/shop3-under10-audit.json'
REVERIFY=ROOT/'output/diagnostics/pivotandpage-gallery-final-current-reverify.json'
OUT=ROOT/'output/diagnostics/pivotandpage-gallery-final-audit.json'
OUTMD=ROOT/'output/diagnostics/pivotandpage-gallery-final-audit.md'
NAMES=[f'etsy_{i:02d}.jpg' for i in range(1,11)]

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
base=json.loads(BASE.read_text()); rv=json.loads(REVERIFY.read_text())
rvmap={r['product']:r for r in rv['results']}
wb=openpyxl.load_workbook(SHOP/'Etsy_SEO_Generator.xlsx',read_only=True,data_only=True); ws=wb['Listings']
rowmap={str(ws.cell(r,2).value or '').strip():r for r in range(4,ws.max_row+1) if str(ws.cell(r,2).value or '').strip()}
results=[]
for t in base['missing']:
    f=t['folder']; root=SHOP/f; row=rowmap.get(f); issues=[]
    files=sorted(p for p in (root/'images').iterdir() if p.is_file() and p.suffix.lower() in {'.jpg','.jpeg','.png','.webp'})
    if [p.name for p in files]!=NAMES: issues.append('image inventory is not canonical 10')
    hashes={}
    for p in files:
        try:
            with Image.open(p) as im: im.verify()
            with Image.open(p) as im:
                if im.size!=(2000,2000): issues.append(f'{p.name} size={im.size}')
                if im.format!='JPEG': issues.append(f'{p.name} format={im.format}')
            hashes[p.name]=sha(p)
        except Exception as e: issues.append(f'{p.name} decode={e}')
    if row is None: issues.append('workbook row missing')
    elif ws.cell(row,7).value!='; '.join(NAMES): issues.append('workbook image inventory mismatch')
    mp=root/'source_catalog/listing-gallery.json'; manifest={}
    if not mp.is_file(): issues.append('listing-gallery.json missing')
    else:
        manifest=json.loads(mp.read_text())
        if int(manifest.get('after_count') or 0)!=10: issues.append('manifest after_count != 10')
        bp=ROOT/str(manifest.get('backup') or '')
        if not bp.is_dir(): issues.append('backup missing')
        mh={x.get('file'):x.get('sha256') for x in manifest.get('gallery') or []}
        for name,digest in hashes.items():
            if mh.get(name)!=digest: issues.append(f'manifest sha mismatch {name}')
    ev=rvmap.get(f)
    if not ev or ev.get('status')!='PASS': issues.append('fresh dashboard reverify missing/fail')
    else:
        checks=ev.get('dashboard',{}).get('checksums') or []
        if len(checks)!=10: issues.append('fresh dashboard checksum evidence != 10')
        by={int(c['image']):c['sha256'] for c in checks}
        for i,name in enumerate(NAMES,1):
            if hashes.get(name)!=by.get(i): issues.append(f'fresh dashboard sha mismatch {name}')
    results.append({'product':f,'title':t.get('title'),'before':t.get('count'),'after':len(files),'row':row,'backup':manifest.get('backup') if manifest else None,'manifest':str(mp.relative_to(ROOT)) if mp.exists() else None,'status':'PASS' if not issues else 'FAIL','issues':issues})
wb.close()
# Full catalog count check.
wb=openpyxl.load_workbook(SHOP/'Etsy_SEO_Generator.xlsx',read_only=True,data_only=True); ws=wb['Listings']; dist=Counter(); under=[]; catalog=0
for r in range(4,ws.max_row+1):
    f=str(ws.cell(r,2).value or '').strip(); root=SHOP/f
    if not f.startswith('product-') or not root.is_dir(): continue
    catalog+=1; cnt=sum(1 for p in (root/'images').iterdir() if p.is_file() and p.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}); dist[cnt]+=1
    if cnt<10: under.append({'product':f,'count':cnt})
wb.close()
# Every gallery run recorded zero newly-created queue IDs.
newops=[]
for path in (ROOT/'output/diagnostics').glob('pivotandpage-gallery-*.json'):
    if path in {OUT,REVERIFY}: continue
    try: d=json.loads(path.read_text())
    except Exception: continue
    for cid in d.get('new_operation_command_ids') or []: newops.append({'report':path.name,'command_id':cid})
payload={'baseline_targets':len(results),'pass':sum(r['status']=='PASS' for r in results),'fail':sum(r['status']!='PASS' for r in results),'catalog_products':catalog,'count_distribution':dict(sorted(dist.items())),'under_10':under,'fresh_dashboard_reverify':{'targets':rv.get('targets'),'passed':rv.get('passed'),'failed':rv.get('blocked'),'active_shop':rv.get('active_shop')},'new_operation_command_ids':newops,'results':results}
OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
lines=['# PivotAndPage Furniture Gallery — Final Audit','',f"- Baseline targets: **{payload['baseline_targets']}**",f"- PASS: **{payload['pass']}**",f"- FAIL: **{payload['fail']}**",f"- Catalog products: **{catalog}**",f"- Under 10 images now: **{len(under)}**",f"- Fresh dashboard checksum reverify: **{rv.get('passed')}/{rv.get('targets')}**",f"- New operation IDs created by gallery runs: **{len(newops)}**",'', '| Product | Before | After | Row | QA | Backup |','|---|---:|---:|---:|---|---|']
for r in results: lines.append(f"| {r['product']} | {r['before']} | {r['after']} | {r['row']} | {r['status']} | {r['backup'] or '—'} |")
OUTMD.write_text('\n'.join(lines)+'\n')
print(json.dumps({'baseline_targets':payload['baseline_targets'],'pass':payload['pass'],'fail':payload['fail'],'catalog_products':catalog,'distribution':dict(sorted(dist.items())),'under_10':under,'fresh_dashboard':payload['fresh_dashboard_reverify'],'new_operation_ids':newops,'json':str(OUT.relative_to(ROOT)),'md':str(OUTMD.relative_to(ROOT))},ensure_ascii=False,indent=2))
raise SystemExit(0 if payload['fail']==0 and not under and not newops else 1)
