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
from crawler_recovery import is_page_crash, recover_page, should_recycle_page

ROOT = Path(os.environ.get("COLLABSTR_ROOT", "/Users/aaronnguyen/Developer/Etsy"))
LISTING_URL = os.environ.get(
    "COLLABSTR_LISTING_URL",
    "https://collabstr.com/influencers?ph_id=1715618&p=tiktok&c=Beauty",
)
CDP = os.environ.get("COLLABSTR_CDP", "http://127.0.0.1:9222")
TARGET = int(os.environ.get("COLLABSTR_TARGET", "13909"))
PROFILE_RECYCLE_EVERY = max(10, int(os.environ.get("COLLABSTR_PROFILE_RECYCLE_EVERY", "25")))
LISTING_RECYCLE_EVERY = max(5, int(os.environ.get("COLLABSTR_LISTING_RECYCLE_EVERY", "25")))
OUT = ROOT / "output/collabstr_tiktok_beauty_crawl.jsonl"
URLS = ROOT / "output/collabstr_tiktok_beauty_urls.jsonl"
PROGRESS = ROOT / "output/collabstr_tiktok_beauty_progress.json"

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
    is_beauty: /beauty/i.test(document.title + ' ' + body)
  };
}'''




def is_tiktok_profile_url(url: str) -> bool:
    """Return whether a URL is a single-creator Collabstr TikTok profile."""
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    segments = [segment.lower() for segment in parsed.path.split("/") if segment]
    reserved = {"influencers", "login", "signup", "register", "pricing", "campaigns"}
    return (
        parsed.scheme in {"http", "https"}
        and host == "collabstr.com"
        and len(segments) == 1
        and segments[0] not in reserved
        and parse_qs(parsed.query).get("p", [""])[0].lower() == "tiktok"
    )


def canonical_profile(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(("https", "collabstr.com", parsed.path.rstrip("/"), "", "p=tiktok", ""))


def key(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower() + parsed.path.rstrip('/').lower()


def pending_profile_urls(discovered, done, attempted_this_run):
    """Keep manifest backlog eligible until it is done or attempted in this run."""
    excluded = set(done) | set(attempted_this_run)
    pending = []
    seen = set()
    for url in discovered:
        if not is_tiktok_profile_url(url):
            continue
        profile_url = canonical_profile(url)
        profile_key = key(profile_url)
        if profile_key not in excluded and profile_key not in seen:
            pending.append(profile_url)
            seen.add(profile_key)
    return pending


def should_pause_at_pagination_boundary(discovered, done, attempted_this_run):
    """Do not abandon already discovered profiles when deeper pages clamp."""
    return not pending_profile_urls(discovered, done, attempted_this_run)


def listing_page(page_number: int) -> str:
    parsed = urlparse(LISTING_URL)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if page_number > 1:
        query['pg'] = [str(page_number)]
    else:
        query.pop('pg', None)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query, doseq=True), parsed.fragment))


def effective_listing_page(url: str) -> int:
    """Return the page number Collabstr actually served after redirects."""
    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    try:
        return int(query.get('pg', ['1'])[0])
    except (TypeError, ValueError):
        return 1


def listing_signature(urls):
    """Return a stable signature for detecting repeated listing content."""
    return tuple(key(url) for url in urls)


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
        if not url or not is_tiktok_profile_url(url):
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
        if row.get('source_url')
        and is_tiktok_profile_url(row.get('source_url', ''))
        and row.get('status') in ('ok', 'blocked')
    }


def append_jsonl(path: Path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def save_progress(
    page_number,
    discovered,
    processed,
    ok,
    errors,
    blocked,
    status='running',
    pause_reason=None,
    effective_page=None,
):
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
    if pause_reason:
        payload['pause_reason'] = pause_reason
    if effective_page is not None:
        payload['effective_listing_page'] = effective_page
    PROGRESS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


async def extract_listing_urls(page):
    hrefs = await page.locator('a').evaluate_all('els => els.map(a => a.href)')
    urls = []
    seen = set()
    for href in hrefs:
        if not is_tiktok_profile_url(href):
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
        attempted_this_run = set()
        consecutive_profile_crashes = 0
        consecutive_listing_crashes = 0
        listing_navigations = 0
        profile_navigations = 0
        seen_listing_signatures = {}
        terminal_status = 'stopped'
        terminal_pause_reason = None
        terminal_effective_page = None

        while len(done) < TARGET:
            url = listing_page(page_number)
            try:
                await listing.goto(url, wait_until='domcontentloaded', timeout=60000)
                listing_navigations += 1
                await listing.wait_for_timeout(2500)
                effective_page = effective_listing_page(listing.url)
                title = await listing.title()
                body = (await listing.locator('body').inner_text(timeout=15000))[:3000]
                if any(x in (title + ' ' + body).lower() for x in ['welcome back', 'log in with email', 'verify you are human', 'checking your browser', 'just a moment']):
                    blocked += 1
                    save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked, 'blocked')
                    print(json.dumps({'status': 'blocked', 'page': page_number, 'url': url}), flush=True)
                    break

                boundary_reason = None
                if effective_page != page_number:
                    reason = (
                        f'Collabstr served pg={effective_page} for requested pg={page_number}; '
                        'pagination boundary detected without advancing.'
                    )
                    terminal_status = 'paused'
                    terminal_pause_reason = reason
                    terminal_effective_page = effective_page
                    boundary_reason = reason
                    if should_pause_at_pagination_boundary(discovered, done, attempted_this_run):
                        save_progress(
                            page_number,
                            len(discovered),
                            len(done),
                            new_ok,
                            new_errors,
                            blocked,
                            'paused',
                            pause_reason=reason,
                            effective_page=effective_page,
                        )
                        print(json.dumps({
                            'status': 'paused',
                            'reason': 'pagination_redirect',
                            'requested_page': page_number,
                            'effective_page': effective_page,
                            'url': listing.url,
                        }), flush=True)
                        break
                    page_urls = []
                    print(json.dumps({
                        'status': 'pagination_boundary_backlog',
                        'requested_page': page_number,
                        'effective_page': effective_page,
                        'pending_manifest_profiles': len(pending_profile_urls(discovered, done, attempted_this_run)),
                    }), flush=True)
                else:
                    page_urls = await extract_listing_urls(listing)
                    signature = listing_signature(page_urls)
                    repeated_page = seen_listing_signatures.get(signature)
                    if repeated_page is not None and repeated_page != page_number:
                        reason = (
                            f'Listing content for pg={page_number} matches pg={repeated_page}; '
                            'pagination did not advance.'
                        )
                        terminal_status = 'paused'
                        terminal_pause_reason = reason
                        terminal_effective_page = effective_page
                        boundary_reason = reason
                        if should_pause_at_pagination_boundary(discovered, done, attempted_this_run):
                            save_progress(
                                page_number,
                                len(discovered),
                                len(done),
                                new_ok,
                                new_errors,
                                blocked,
                                'paused',
                                pause_reason=reason,
                                effective_page=effective_page,
                            )
                            print(json.dumps({
                                'status': 'paused',
                                'reason': 'repeated_listing_content',
                                'requested_page': page_number,
                                'previous_page': repeated_page,
                            }), flush=True)
                            break
                        page_urls = []
                        print(json.dumps({
                            'status': 'repeated_listing_backlog',
                            'requested_page': page_number,
                            'previous_page': repeated_page,
                            'pending_manifest_profiles': len(pending_profile_urls(discovered, done, attempted_this_run)),
                        }), flush=True)
                    else:
                        seen_listing_signatures[signature] = page_number

                consecutive_listing_crashes = 0
                before = len(discovered)
                existing = {key(u) for u in discovered}
                for profile_url in page_urls:
                    if key(profile_url) not in existing:
                        append_jsonl(URLS, {'source_url': profile_url, 'listing_url': LISTING_URL, 'listing_page': page_number})
                        discovered.append(profile_url)
                        existing.add(key(profile_url))
                print(json.dumps({'listing_page': page_number, 'page_profiles': len(page_urls), 'new_discovered': len(discovered) - before, 'discovered_total': len(discovered)}), flush=True)
                if should_recycle_page(listing_navigations, LISTING_RECYCLE_EVERY):
                    listing = await recover_page(ctx, listing)
                    listing_navigations = 0
                    print(json.dumps({
                        'status': 'listing_page_recycled',
                        'listing_page': page_number,
                        'every_navigations': LISTING_RECYCLE_EVERY,
                    }), flush=True)

                batch = pending_profile_urls(discovered, done, attempted_this_run)
                if limit:
                    batch = batch[:limit]
                for profile_url in batch:
                    if len(done) >= TARGET:
                        break
                    attempted_this_run.add(key(profile_url))
                    profile_attempt = 0
                    while True:
                        profile_attempt += 1
                        try:
                            await profile.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
                            profile_navigations += 1
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
                            consecutive_profile_crashes = 0
                            print(json.dumps({'status': 'ok', 'n': len(done), 'url': profile_url, 'images': len(live.get('gallery') or []), 'videos': len(live.get('portfolio') or []), 'packages': len(live.get('offers') or [])}), flush=True)
                            break
                        except Exception as exc:
                            append_jsonl(OUT, {
                                'source_url': profile_url,
                                'listing_url': LISTING_URL,
                                'listing_page': page_number,
                                'status': 'error',
                                'retryable': True,
                                'error': type(exc).__name__ + ':' + str(exc)[:300],
                            })
                            new_errors += 1
                            print(json.dumps({'status': 'error', 'url': profile_url, 'error': type(exc).__name__}), flush=True)
                            if is_page_crash(exc):
                                consecutive_profile_crashes += 1
                                profile = await recover_page(ctx, profile)
                                print(json.dumps({'status': 'profile_page_recreated', 'url': profile_url, 'attempt': profile_attempt}), flush=True)
                                save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked, 'retrying')
                                if consecutive_profile_crashes >= 3:
                                    save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked, 'stopped')
                                    print(json.dumps({'status': 'stopped', 'reason': 'repeated_profile_page_crashes'}), flush=True)
                                    return
                                if profile_attempt == 1:
                                    await asyncio.sleep(2)
                                    continue
                            else:
                                consecutive_profile_crashes = 0
                            break
                    save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked)
                    if should_recycle_page(profile_navigations, PROFILE_RECYCLE_EVERY):
                        profile = await recover_page(ctx, profile)
                        profile_navigations = 0
                        print(json.dumps({
                            'status': 'profile_page_recycled',
                            'processed_profiles': len(done),
                            'every_navigations': PROFILE_RECYCLE_EVERY,
                        }), flush=True)
                    await profile.wait_for_timeout(3000)
                    if len(done) % 100 == 0:
                        await profile.wait_for_timeout(30000)
                if limit:
                    break
                if len(done) >= TARGET:
                    break
                if boundary_reason:
                    retryable_next_run = sum(
                        1 for profile_url in discovered
                        if key(profile_url) not in done and key(profile_url) in attempted_this_run
                    )
                    save_progress(
                        page_number,
                        len(discovered),
                        len(done),
                        new_ok,
                        new_errors,
                        blocked,
                        'paused',
                        pause_reason=boundary_reason,
                        effective_page=effective_page,
                    )
                    print(json.dumps({
                        'status': 'paused',
                        'reason': 'pagination_boundary_after_manifest_drain',
                        'requested_page': page_number,
                        'effective_page': effective_page,
                        'processed_profiles': len(done),
                        'retryable_next_run': retryable_next_run,
                    }), flush=True)
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
                if is_page_crash(exc):
                    consecutive_listing_crashes += 1
                    listing = await recover_page(ctx, listing)
                    profile = await recover_page(ctx, profile)
                    print(json.dumps({'status': 'listing_and_profile_pages_recreated', 'page': page_number, 'attempt': consecutive_listing_crashes}), flush=True)
                    if consecutive_listing_crashes >= 3:
                        save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked, 'stopped')
                        print(json.dumps({'status': 'stopped', 'reason': 'repeated_listing_page_crashes'}), flush=True)
                        return
                    await asyncio.sleep(10)
                    continue
                if type(exc).__name__ == 'TimeoutError':
                    await asyncio.sleep(10)
                    continue
                save_progress(page_number, len(discovered), len(done), new_ok, new_errors, blocked, 'error')
                break
        status = 'complete' if len(done) >= TARGET else terminal_status
        save_progress(
            page_number,
            len(discovered),
            len(done),
            new_ok,
            new_errors,
            blocked,
            status,
            pause_reason=terminal_pause_reason,
            effective_page=terminal_effective_page,
        )
        print(json.dumps({'status': status, 'discovered_profiles': len(discovered), 'processed_profiles': len(done), 'ok': new_ok, 'errors': new_errors, 'blocked': blocked, 'output': str(OUT)}), flush=True)
        await profile.close()


if __name__ == '__main__':
    asyncio.run(main())
