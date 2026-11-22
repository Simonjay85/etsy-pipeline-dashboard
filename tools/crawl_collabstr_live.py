#!/usr/bin/env python3
import asyncio, csv, json, re, sys, time
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path('/Users/aaronnguyen/Developer/Etsy')
CSV_PATH = ROOT / 'collabstr_tiktok_lifestyle_kol.csv'
OUT = ROOT / 'output/collabstr_live_crawl_full.jsonl'
PROGRESS = ROOT / 'output/collabstr_live_crawl_full_progress.json'
CDP = 'http://127.0.0.1:9222'

EXTRACT_JS = r'''() => {
  const clean = v => (v || '').replace(/\s+/g, ' ').trim();
  const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')].map(s => {
    try { return JSON.parse(s.textContent); } catch (_) { return null; }
  }).filter(Boolean);
  const flatten = x => Array.isArray(x) ? x.flatMap(flatten) : (x && typeof x === 'object' ? [x, ...Object.values(x).flatMap(flatten)] : []);
  const ld = flatten(scripts);
  const images = [...document.images].map(x => x.currentSrc || x.src).filter(Boolean);
  const videos = [...document.querySelectorAll('video, video source')].map(x => x.currentSrc || x.src || x.getAttribute('src')).filter(Boolean);
  const links = [...document.querySelectorAll('a[href]')].map(x => x.href).filter(Boolean);
  const text = document.body ? document.body.innerText : '';
  const person = ld.find(x => x['@type'] === 'Person') || {};
  const aggregate = ld.find(x => x['@type'] === 'AggregateRating') || person.aggregateRating || {};
  const products = ld.filter(x => x['@type'] === 'Product');
  const offers = products.flatMap(x => Array.isArray(x.offers) ? x.offers : (x.offers ? [x.offers] : []));
  const sellerMedia = xs => [...new Set(xs.filter(x => /cloudfront\.net\/sellers\//i.test(x) && !/websiteImages/i.test(x)))];
  const social = [...new Set(links.filter(x => /instagram\.com|tiktok\.com|youtube\.com|youtu\.be/i.test(x)))];
  return {
    title: document.title,
    heading: clean(document.querySelector('h1')?.innerText || document.querySelector('h2')?.innerText),
    name: clean(person.name || ''),
    bio: clean(person.description || ''),
    location: clean(person.homeLocation?.name || person.address?.addressLocality || ''),
    social,
    rating: aggregate,
    products,
    offers,
    gallery: sellerMedia(images).slice(0, 12),
    portfolio: sellerMedia(videos).slice(0, 24),
    visible_text: text.slice(0, 50000),
    image_count: sellerMedia(images).length,
    video_count: sellerMedia(videos).length,
    page_url: location.href
  };
}'''

def load_rows():
    with CSV_PATH.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))

def load_done():
    done = set()
    if OUT.exists():
        with OUT.open(encoding='utf-8') as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get('source_url'):
                        done.add(d['source_url'])
                except Exception:
                    pass
    return done

def append_record(rec):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open('a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')

def save_progress(total, done, ok, errors, blocked):
    PROGRESS.write_text(json.dumps({
        'source_csv': str(CSV_PATH), 'target_profiles': total,
        'crawled': done, 'ok': ok, 'errors': errors,
        'blocked': blocked, 'remaining': total - done,
        'output': str(OUT), 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')
    }, ensure_ascii=False, indent=2), encoding='utf-8')

async def main():
    rows = load_rows(); done_urls = load_done()
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else len(rows)
    todo = [r for r in rows[start:start+limit] if r.get('profileUrl') or r.get('profile_url')]
    todo = [r for r in todo if (r.get('profileUrl') or r.get('profile_url')) not in done_urls]
    ok = len(done_urls); errors = 0; blocked = 0
    print(json.dumps({'total': len(rows), 'already_done': len(done_urls), 'to_process': len(todo), 'start': start, 'limit': limit}))
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        for n, row in enumerate(todo, 1):
            url = row.get('profileUrl') or row.get('profile_url')
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                await page.wait_for_timeout(1800)
                title = await page.title()
                body = (await page.locator('body').inner_text(timeout=10000))[:1200]
                if 'Just a moment' in title or 'Verify you are human' in body or 'Checking your browser' in body:
                    blocked += 1
                    rec = {'source_url': url, 'username': row.get('username',''), 'status': 'blocked', 'title': title, 'error': 'browser_challenge'}
                    append_record(rec); print(json.dumps({'n': n, 'status': 'blocked', 'url': url}), flush=True)
                    await page.wait_for_timeout(30000)
                    continue
                data = await page.evaluate(EXTRACT_JS)
                rec = {'source_url': url, 'username': row.get('username',''), 'csv': row, 'status': 'ok', 'live': data}
                append_record(rec); ok += 1
                print(json.dumps({'n': n, 'status': 'ok', 'url': url, 'images': data.get('image_count'), 'videos': data.get('video_count')}), flush=True)
            except Exception as e:
                errors += 1
                append_record({'source_url': url, 'username': row.get('username',''), 'status': 'error', 'error': type(e).__name__ + ': ' + str(e)[:500]})
                print(json.dumps({'n': n, 'status': 'error', 'url': url, 'error': type(e).__name__}), flush=True)
            if n % 10 == 0:
                save_progress(len(rows), len(done_urls) + n, ok, errors, blocked)
            await page.wait_for_timeout(700)
        await page.close()
    save_progress(len(rows), len(done_urls) + len(todo), ok, errors, blocked)
    print(json.dumps({'finished': True, 'ok': ok, 'errors': errors, 'blocked': blocked, 'output': str(OUT)}))

if __name__ == '__main__':
    asyncio.run(main())
