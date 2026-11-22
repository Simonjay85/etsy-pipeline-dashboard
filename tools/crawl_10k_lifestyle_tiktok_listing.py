#!/usr/bin/env python3
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.async_api import async_playwright

from collabstr_page_selection import select_page_indexes

ROOT = Path(os.environ.get("COLLABSTR_ROOT", "/Users/aaronnguyen/Developer/Etsy"))
LISTING_URL = os.environ.get(
    "COLLABSTR_LISTING_URL",
    "https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Lifestyle",
)
CDP = os.environ.get("COLLABSTR_CDP", "http://127.0.0.1:9222")
TARGET = int(os.environ.get("COLLABSTR_TARGET", "10000"))
OUT = ROOT / "output/collabstr_10k_lifestyle_tiktok_crawl.jsonl"
URLS = ROOT / "output/collabstr_10k_lifestyle_tiktok_urls.jsonl"
PROGRESS = ROOT / "output/collabstr_10k_lifestyle_tiktok_progress.json"

PROFILE_EXTRACT = r'''() => {
  const clean = v => (v || '').replace(/\s+/g, ' ').trim();
  const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .map(s => { try { return JSON.parse(s.textContent); } catch (_) { return null; } })
    .filter(Boolean);
  const flat = x => Array.isArray(x) ? x.flatMap(flat) :
    (x && typeof x === 'object' ? [x, ...Object.values(x).flatMap(flat)] : []);
  const ld = flat(scripts);
  const person = ld.find(x => x['@type'] === 'Person') || {};
  const rating = ld.find(x => x['@type'] === 'AggregateRating') || person.aggregateRating || {};
  const reviews = ld.filter(x => x['@type'] === 'Review');
  const faq = ld.find(x => x['@type'] === 'FAQPage') || {};
  const offers = [];
  for (const x of ld) {
    const type = Array.isArray(x['@type']) ? x['@type'] : [x['@type']];
    if (type.includes('Service') || type.includes('Product') || type.includes('Offer')) {
      if (x.offers) offers.push(x);
      if (type.includes('Offer')) offers.push(x);
    }
  }
  const gallery = [...new Set([...document.images]
    .map(x => x.currentSrc || x.src)
    .filter(x => x && x.includes("cloudfront.net/sellers/") && !x.includes("websiteImages")))].slice(0, 20);
  const portfolio = [...new Set([...document.querySelectorAll("video, video source")]
    .map(x => x.currentSrc || x.src)
    .filter(x => x && x.includes("cloudfront.net/sellers/")))].slice(0, 40);
  const social = [...new Set([...document.querySelectorAll("a[href]")]
    .map(x => x.href)
    .filter(x => /instagram|tiktok|youtube|twitter|x.com/i.test(x)))];
  const body = document.body ? document.body.innerText : '';
  const heading = clean(document.querySelector('h1')?.innerText || document.querySelector('h2')?.innerText);
  const textMetrics = {};
  for (const label of ['followers', 'average views', 'engagement', 'audience', 'location', 'gender', 'age']) {
    const rx = new RegExp(label + '.{0,180}', 'ig');
    textMetrics[label] = [...body.matchAll(rx)].slice(0, 5).map(m => clean(m[0]));
  }
  return {
    title: document.title,
    heading,
    name: clean(person.name || ''),
    bio: clean(person.description || ''),
    location: clean(person.homeLocation?.name || person.address?.addressLocality || ''),
    person,
    rating,
    reviews,
    faq,
    offers,
    social,
    gallery,
    portfolio,
    text_metrics: textMetrics,
    visible_text: body.slice(0, 80000),
    is_tiktok: /tiktok/i.test(document.title + ' ' + body),
    is_lifestyle: /lifestyle/i.test(document.title + ' ' + body)
  };
}'''


def canonical_profile(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', 'p=tiktok', ''))


def key(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower() + parsed.path.rstrip('/').lower()


def listing_page(page_number: int) -> str:
    parsed = urlparse(LISTING_URL)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if page_number > 1:
        query['pg'] = [str(page_number)]
    else:
        query.pop('pg', None)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query, doseq=True), parsed.fragment))


def read_jsonl(path: Path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').split('\n'):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def discovered_urls():
    rows = read_jsonl(URLS)
    out = []
    seen = set()
    for row in rows:
        url = row.get('source_url') if isinstance(row, dict) else row
        if not url:
            continue
        url = canonical_profile(url)
        if key(url) not in seen:
            seen.add(key(url))
            out.append(url)
    return out


def processed_keys():
    return {
        key(row.get('source_url', ''))
        for row in read_jsonl(OUT)
        if row.get('source_url') and row.get('status') in ('ok', 'blocked')
    }


def append_jsonl(path: Path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def save_progress(page_number, discovered, processed, ok, errors, blocked, status='running'):
    payload = {
        'target': TARGET,
        'listing_url': LISTING_URL,
        'listing_page': page_number,
        'discovered_profiles': discovered,
        'processed_profiles': processed,
        'ok': ok,
        'errors': errors,
        'blocked': blocked,
        'remaining': max(0, TARGET - processed),
        'status': status,
        'output': str(OUT),
        'urls_output': str(URLS),
        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    PROGRESS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


async def extract_listing_urls(page):
    hrefs = await page.locator('a').evaluate_all('els => els.map(a => a.href)')
    urls = []
    seen = set()
    for href in hrefs:
        if not href or 'collabstr.com/' not in href:
            continue
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        if query.get('p', [''])[0].lower() != 'tiktok':
            continue
        if parsed.path.count('/') != 1 or not parsed.path.strip('/'):
            continue
        profile = canonical_profile(href)
        if key(profile) not in seen:
            seen.add(key(profile))
            urls.append(profile)
    return urls


async def main():
    discovered = discovered_urls()
    done = processed_keys()
    progress = {}
    if PROGRESS.exists():
        try:
            progress = json.loads(PROGRESS.read_text(encoding='utf-8'))
        except Exception:
            progress = {}
    page_number = max(1, int(progress.get('listing_page', 1)))
    if progress.get('status') == 'running' and page_number > 1:
        page_number = max(1, page_number - 1)
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    new_ok = new_errors = blocked = 0

    print(json.dumps({
        'listing_url': LISTING_URL,
        'listing_page': page_number,
        'discovered_profiles': len(discovered),
        'processed_profiles': len(done),
        'target': TARGET,
    }), flush=True)

    async with async_playwright() as pw:
        browser = None
        for attempt in range(1, 4):
            try:
                browser = await pw.chromium.connect_over_cdp(CDP, timeout=30000)
                break
            except Exception as exc:
                print(json.dumps({'status': 'cdp_connect_retry', 'attempt': attempt, 'error': type(exc).__name__}), flush=True)
                if attempt == 3:
                    raise
                await asyncio.sleep(5 * attempt)
        assert browser is not None
        ctx = browser.contexts[0]
        pages = ctx.pages
        listing_index, profile_index = select_page_indexes([page.url for page in pages], LISTING_URL)
        listing = pages[listing_index] if listing_index is not None else await ctx.new_page()
        profile = pages[profile_index] if profile_index is not None else await ctx.new_page()

        while len(done) < TARGET:
            url = listing_page(page_number)
            try:
                await listing.goto(url, wait_until='domcontentloaded', timeout=60000)
                await listing.wait_for_timeout(2500)
                title = await listing.title()
                body = (await listing.locator('body').inner_text(timeout=15000))[:3000]
                if any(x in (title + ' ' + body).lower() for x in ['welcome back', 'log in with email', 'verify you are human', 'checking your browser', 'just a moment']):
                    append_jsonl(OUT, {'source_url': url, 'listing_url': LISTING_URL, 'status': 'blocked', 'error': 'login_or_browser_challenge'})
                    blocked += 1
                    save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked, 'blocked')
                    print(json.dumps({'status': 'blocked', 'page': page_number, 'url': url}), flush=True)
                    break
                page_urls = await extract_listing_urls(listing)
                before = len(discovered)
                existing = {key(u) for u in discovered}
                for profile_url in page_urls:
                    if key(profile_url) not in existing:
                        append_jsonl(URLS, {'source_url': profile_url, 'listing_url': LISTING_URL, 'listing_page': page_number})
                        discovered.append(profile_url)
                        existing.add(key(profile_url))
                print(json.dumps({'listing_page': page_number, 'page_profiles': len(page_urls), 'new_discovered': len(discovered) - before, 'discovered_total': len(discovered)}), flush=True)

                batch = [u for u in discovered if key(u) not in done]
                if limit:
                    batch = batch[:limit]
                for profile_url in batch:
                    if len(done) >= TARGET:
                        break
                    try:
                        await profile.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
                        await profile.wait_for_timeout(2200)
                        title = await profile.title()
                        body = (await profile.locator('body').inner_text(timeout=15000))[:1800]
                        if any(x in (title + ' ' + body).lower() for x in ['welcome back', 'log in with email', 'verify you are human', 'checking your browser', 'just a moment']):
                            append_jsonl(OUT, {'source_url': profile_url, 'listing_url': LISTING_URL, 'status': 'blocked', 'error': 'login_or_browser_challenge'})
                            blocked += 1
                            print(json.dumps({'status': 'blocked', 'url': profile_url}), flush=True)
                            save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked, 'blocked')
                            return
                        live = await profile.evaluate(PROFILE_EXTRACT)
                        row = {
                            'source_url': profile_url,
                            'listing_url': LISTING_URL,
                            'listing_page': page_number,
                            'status': 'ok',
                            'live': live,
                        }
                        append_jsonl(OUT, row)
                        done.add(key(profile_url))
                        new_ok += 1
                        print(json.dumps({'status': 'ok', 'n': len(done), 'url': profile_url, 'images': len(live.get('gallery') or []), 'videos': len(live.get('portfolio') or []), 'packages': len(live.get('offers') or [])}), flush=True)
                    except Exception as exc:
                        append_jsonl(OUT, {'source_url': profile_url, 'listing_url': LISTING_URL, 'listing_page': page_number, 'status': 'error', 'error': type(exc).__name__ + ':' + str(exc)[:300]})
                        done.add(key(profile_url))
                        new_errors += 1
                        print(json.dumps({'status': 'error', 'url': profile_url, 'error': type(exc).__name__}), flush=True)
                    save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked)
                    await profile.wait_for_timeout(3000)
                    if len(done) % 100 == 0:
                        await profile.wait_for_timeout(30000)
                if limit:
                    break
                if len(done) >= TARGET:
                    break
                page_number += 1
                save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked)
                await listing.wait_for_timeout(5000)
                if page_number % 10 == 0:
                    await listing.wait_for_timeout(30000)
            except Exception as exc:
                error_text = type(exc).__name__ + ':' + str(exc)[:300]
                print(json.dumps({'status': 'listing_error', 'page': page_number, 'error': error_text}), flush=True)
                save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked, 'retrying')
                if type(exc).__name__ in ('TimeoutError', 'TargetClosedError'):
                    try:
                        await listing.wait_for_timeout(10000)
                    except Exception:
                        listing = await ctx.new_page()
                    continue
                save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked, 'error')
                break
        status = 'complete' if len(done) >= TARGET else 'stopped'
        save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked, status)
        print(json.dumps({'status': status, 'discovered_profiles': len(discovered), 'processed_profiles': len(done), 'ok': new_ok, 'errors': new_errors, 'blocked': blocked, 'output': str(OUT)}), flush=True)
        await profile.close()


asyncio.run(main())
