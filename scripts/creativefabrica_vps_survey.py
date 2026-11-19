#!/usr/bin/env python3
"""Resume the public CF survey through VPS Chromium, using the existing CSV/checkpoint.

One process owns the output lock. CSV is committed before the checkpoint; recovery
deduplicates by product id. Authentication/challenge/schema failures stop for review.
"""
import argparse
import csv
import errno
import fcntl
import json
import math
import os
import re
import signal
import socket
import statistics
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import creativefabrica_kdp_favorites_worker as legacy

STOP_REQUESTED = False
CONTROL_FILENAME = 'anna_control.json'
CONTROL_MAX_BYTES = 64 * 1024
CONTROL_FIELDS = frozenset({'revision', 'delay_seconds', 'daily_limit'})


class ReviewRequired(RuntimeError):
    pass


class ProductUnavailable(ReviewRequired):
    def __init__(self, code, url, reason=None):
        self.code = code
        super().__init__(f'{reason or "HTTP " + str(code)}: {url}')


class ControlFileError(ValueError):
    """The optional ANNA control file is present but unsafe or invalid."""


def _reject_json_constant(value):
    raise ControlFileError(f'non-finite JSON constant: {value}')


def _reject_duplicate_control_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ControlFileError(f'duplicate control key: {key}')
        value[key] = item
    return value


def _strict_int(value, name, lower, upper=None):
    if type(value) is not int:  # bool is an int subclass, but is not valid here.
        raise ControlFileError(f'{name} must be a strict integer')
    if value < lower or (upper is not None and value > upper):
        bound = f'{lower}..{upper}' if upper is not None else f'>={lower}'
        raise ControlFileError(f'{name} must be in {bound}')
    return value


def _strict_number(value, name, lower, upper):
    if type(value) not in (int, float):  # bool is also rejected explicitly.
        raise ControlFileError(f'{name} must be a strict number')
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite or value < lower or value > upper:
        raise ControlFileError(f'{name} must be finite and in {lower}..{upper}')
    return value


def validate_control(value):
    """Validate and normalize one ANNA hot-control JSON object."""
    if type(value) is not dict:
        raise ControlFileError('control must be a JSON object')
    missing = CONTROL_FIELDS - value.keys()
    if missing:
        raise ControlFileError(f'missing control field(s): {", ".join(sorted(missing))}')
    unknown = value.keys() - CONTROL_FIELDS
    if unknown:
        raise ControlFileError(f'unknown control field(s): {", ".join(sorted(unknown))}')
    return {
        'revision': _strict_int(value['revision'], 'revision', 0),
        'delay_seconds': _strict_number(value['delay_seconds'], 'delay_seconds', 10, 300),
        'daily_limit': _strict_int(value['daily_limit'], 'daily_limit', 100, 10000),
    }


def read_control(path, max_bytes=CONTROL_MAX_BYTES):
    """Read an optional control file without following symlinks or large files.

    ``None`` means the file is absent.  A present but malformed file raises
    ``ControlFileError`` so the caller can retain its last known-good values.
    """
    path = Path(path)
    try:
        initial = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ControlFileError(f'cannot inspect control file: {exc}') from exc
    if stat.S_ISLNK(initial.st_mode):
        raise ControlFileError('control file must not be a symlink')
    if not stat.S_ISREG(initial.st_mode):
        raise ControlFileError('control file must be a regular file')
    if initial.st_size > max_bytes:
        raise ControlFileError(f'control file is oversized (>{max_bytes} bytes)')

    flags = os.O_RDONLY
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    flags |= nofollow
    fd = None
    try:
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            # An atomic parent write may replace the file between lstat/open.
            return None
        except OSError as exc:
            if nofollow and exc.errno == errno.ELOOP:
                raise ControlFileError('control file must not be a symlink') from exc
            raise ControlFileError(f'cannot open control file: {exc}') from exc
        opened = os.fstat(fd)
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
            raise ControlFileError('control file must be a regular non-symlink file')
        if opened.st_size > max_bytes:
            raise ControlFileError(f'control file is oversized (>{max_bytes} bytes)')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            raw = stream.read(max_bytes + 1)
    finally:
        if fd is not None:
            os.close(fd)
    if len(raw) > max_bytes:
        raise ControlFileError(f'control file is oversized (>{max_bytes} bytes)')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ControlFileError('control file is not valid UTF-8') from exc
    try:
        value = json.loads(text, parse_constant=_reject_json_constant,
                           object_pairs_hook=_reject_duplicate_control_keys)
    except ControlFileError:
        raise
    except json.JSONDecodeError as exc:
        raise ControlFileError(f'invalid control JSON: {exc.msg}') from exc
    return validate_control(value)


class ControlState:
    """Keep the last known-good hot-control values across polling cycles."""

    def __init__(self, delay_seconds, daily_limit):
        self._args_config = {
            'delay_seconds': delay_seconds,
            'daily_limit': daily_limit,
        }
        self._effective_config = dict(self._args_config)
        self._control_revision = None
        self._last_control_config = None
        self._control_error = None

    @property
    def delay_seconds(self):
        return self._effective_config['delay_seconds']

    @property
    def daily_limit(self):
        return self._effective_config['daily_limit']

    @property
    def effective_config(self):
        return dict(self._effective_config)

    @property
    def control_revision(self):
        # Revision zero is the telemetry baseline before ANNA has supplied a
        # control file.  Keep ``_control_revision`` as None internally so a
        # first valid revision zero is still accepted.
        return 0 if self._control_revision is None else self._control_revision

    @property
    def control_error(self):
        return self._control_error

    def telemetry(self):
        return {
            'effective_config': self.effective_config,
            'control_revision': self.control_revision,
            'control_error': self.control_error,
        }

    def refresh(self, path):
        """Poll ``path`` and return whether telemetry should be republished."""
        before = (self.effective_config, self.control_revision, self.control_error)
        try:
            control = read_control(path)
        except ControlFileError as exc:
            self._control_error = f'control_error: {exc}'
        else:
            if control is None:
                # An absent optional file leaves command-line defaults in effect
                # until a control has been accepted.  After that, keep the
                # accepted values through a transient delete/replace window.
                self._control_error = None
            else:
                revision = control['revision']
                config = {key: control[key] for key in ('delay_seconds', 'daily_limit')}
                if self._control_revision is not None and revision < self._control_revision:
                    self._control_error = (
                        f'control_error: revision rollback from {self._control_revision} to {revision}')
                elif (self._control_revision is not None and
                      revision == self._control_revision and
                      config != self._last_control_config):
                    self._control_error = (
                        f'control_error: revision {revision} changed its configuration')
                else:
                    self._effective_config = config
                    self._control_revision = revision
                    self._last_control_config = dict(config)
                    self._control_error = None
        after = (self.effective_config, self.control_revision, self.control_error)
        return before != after


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_rows(out):
    with (out / legacy.MASTER.name).open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    if len({row['product_id'] for row in rows}) != len(rows):
        raise ReviewRequired('Duplicate product ids in source CSV; reconcile before resuming')
    return rows


def save_rows(out, rows):
    target = out / legacy.MASTER.name
    temporary = target.with_suffix('.csv.tmp')
    with temporary.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=legacy.FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)


def valid(row):
    try:
        return not row.get('error') and float(row['favorite_estimate']) >= 0
    except (ValueError, TypeError, KeyError):
        return False


def write_report(out, rows, current):
    measured = [r for r in rows if valid(r)]
    values = [float(r['favorite_estimate']) for r in measured]
    lines = ['# Creative Fabrica KDP Interiors — VPS survey', '',
             f'- Updated UTC: {current["updated_at"]}',
             f'- Runtime status: {current["status"]}',
             f'- Products retained: {len(rows)}',
             f'- Verified favorite values: {len(values)}',
             f'- Missing values (not zero): {len(rows) - len(values)}',
             f'- Next category page: {current["next_category_page"]}',
             f'- Sum of parsed favorite estimates: {sum(values):,.0f}',
             f'- Median of parsed values: {statistics.median(values) if values else "N/A"}',
             '', 'This is a rolling, partial public-page survey. K/M counts are estimates.',
             'Missing/denied values are excluded from aggregates; see CSV error column.', '',
             '| Product | Displayed favorites | URL |', '|---|---:|---|']
    for row in sorted(measured, key=lambda r: float(r['favorite_estimate']), reverse=True)[:20]:
        title = row['title'].replace('|', '\\|').replace('\n', ' ')
        lines.append(f'| {title} | {row["favorite_display"]} | {row["product_url"]} |')
    target = out / legacy.REPORT.name
    temporary = target.with_suffix('.md.tmp')
    temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    temporary.replace(target)


def checked_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'www.creativefabrica.com':
        raise ReviewRequired('Unexpected target host')
    if not (parsed.path.startswith('/product/') or
            parsed.path.startswith('/subscriptions/graphics/kdp-interiors/')):
        raise ReviewRequired('Unexpected target path')
    return url


def load_page(page, url):
    response = page.goto(checked_url(url), wait_until='domcontentloaded', timeout=60000)
    checked_url(page.url)
    if response is not None and response.status in (403, 404, 410) and urlsplit(url).path.startswith('/product/'):
        raise ProductUnavailable(response.status, url)
    if response is None or response.status in (401, 403, 429):
        raise ReviewRequired(f'Access blocked: HTTP {response.status if response else "unknown"}')
    if response.status >= 500 or response.status in (408, 425):
        raise RuntimeError(f'Transient upstream error: HTTP {response.status}')
    if response.status >= 400:
        raise ReviewRequired(f'Page unavailable: HTTP {response.status}')
    title = page.title().lower()
    if any(text in title for text in ('just a moment', 'access denied', 'verify you are human')):
        raise ReviewRequired('Browser challenge requires review')
    return page.content(), response.status


def reconcile_pending(rows, state):
    good = {row['product_id'] for row in rows if valid(row)}
    unavailable = set(state.get('unavailable_products', {}))
    # Recover failures from the legacy worker, which marked errors as processed.
    candidates = [row for row in rows if not valid(row)] + state.get('pending_products', [])
    pending = {}
    for row in candidates:
        if row['product_id'] not in good and row['product_id'] not in unavailable:
            pending.setdefault(row['product_id'], {k: row[k] for k in ('product_id', 'title', 'product_url')})
    return good, list(pending.values())


def category_terminal(text):
    match = re.search(r'Showing\s+[\d,]+\s*[-–]\s*([\d,]+)\s+of\s+([\d,]+)\s+results', text, re.I)
    return bool(match and int(match[1].replace(',', '')) >= int(match[2].replace(',', '')))


def advance_category(state):
    if state.get('last_page_terminal'):
        state['completed'] = True
    elif state.get('last_page_has_next', True):
        state['next_category_page'] += 1
    else:
        raise ReviewRequired('No next-page link; verify end of catalog before completion')


def run(args):
    from playwright.sync_api import sync_playwright
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    status_path = out / 'vps_status.json'
    control_path = out / CONTROL_FILENAME
    control_state = ControlState(args.delay, args.daily_limit)
    with (out / legacy.LOCK.name).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75
        state_path = out / legacy.STATE.name
        state = json.loads(state_path.read_text())  # Corruption must not reset progress.
        rows = read_rows(out)
        good, pending = reconcile_pending(rows, state)
        # Retried historical errors are separate from the current category page.
        retries = [p for p in pending if p['product_id'] not in
                   {q['product_id'] for q in state.get('pending_products', [])}]
        pending = [p for p in pending if p not in retries]
        processed_this_run = 0
        page = None

        def status(value, error=None):
            current = dict(status=value, error=error, host=socket.gethostname(),
                session_id='20260913_235156_d6e606', executor='cf-kdp-browser-worker',
                updated_at=legacy.now(), collected_total=len(rows), verified_favorites=len(good),
                unavailable_products=len(state.get('unavailable_products', {})),
                next_category_page=state['next_category_page'], processed_this_run=processed_this_run,
                production_write=False, anna_task_id=None,
                effective_config=control_state.effective_config,
                control_revision=control_state.control_revision,
                control_error=control_state.control_error)
            atomic_json(status_path, current)
            write_report(out, rows, current)

        def save():
            state.update(pending_products=pending, processed_ids=sorted(good),
                         last_run_utc=legacy.now(), last_batch_count=processed_this_run)
            atomic_json(state_path, state)

        control_state.refresh(control_path)
        status('starting')
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(args.cdp)
                page = browser.contexts[0].new_page()
                try:
                    while not state.get('completed') or retries:
                        if control_state.refresh(control_path):
                            # Publish accepted hot changes and control errors
                            # before making the next request.
                            status('running')
                        today = datetime.now(timezone.utc).date().isoformat()
                        if state.get('budget_date') != today:
                            state.update(budget_date=today, requests_today=0)
                        if state.get('requests_today', 0) >= control_state.daily_limit:
                            status('waiting_daily_budget')
                            # Re-enter the loop after a short bounded wait so
                            # ANNA can raise the limit and the heartbeat stays
                            # fresh while the worker is at the daily ceiling.
                            time.sleep(30)
                            continue
                        if not retries and not pending:
                            number = state['next_category_page']
                            url = legacy.CATEGORY if number == 1 else f'{legacy.CATEGORY}page/{number}/'
                            body, _ = load_page(page, url)
                            products = legacy.parse_category(body)
                            if not products:
                                # No products could be a parser regression or challenge.
                                raise ReviewRequired('Category has no parseable products; completion is unverified')
                            pending[:] = [p for p in products if p['product_id'] not in good
                                          and p['product_id'] not in state.get('unavailable_products', {})]
                            next_url = f'{legacy.CATEGORY}page/{number + 1}/'
                            state['last_page_has_next'] = page.locator(
                                f'a[href="{next_url}"], a[href="{urlsplit(next_url).path}"]').count() > 0
                            state['last_page_terminal'] = category_terminal(page.inner_text('body'))
                            save()  # Persist discovery before any product request.
                            if not pending:
                                advance_category(state)
                                save()
                                continue
                        product = retries[0] if retries else pending[0]
                        state['requests_today'] = state.get('requests_today', 0) + 1
                        save()  # Account for requests even if this process is interrupted.
                        error = ''
                        try:
                            body, http_status = load_page(page, product['product_url'])
                            raw, numeric = legacy.parse_favorite(body)
                            if numeric is None:
                                raise ProductUnavailable(http_status, product['product_url'], 'favorite_count_not_displayed_or_unparseable')
                            state['consecutive_product_denials'] = 0
                        except ProductUnavailable as exc:
                            # Keep the inaccessible URL as a report gap; do not retry it
                            # every restart or mistake a denied product for zero favorites.
                            raw, numeric, http_status, error = '', None, exc.code, str(exc)
                            state.setdefault('unavailable_products', {})[product['product_id']] = {
                                'url': product['product_url'], 'http_status': exc.code,
                                'reason': str(exc), 'checked_at': legacy.now()}
                            state['consecutive_product_denials'] = (
                                state.get('consecutive_product_denials', 0) + 1 if exc.code == 403 else 0)
                        existing = next((r for r in rows if r['product_id'] == product['product_id']), None)
                        result = dict(product, favorite_display=raw, favorite_estimate=numeric,
                                      http_status=http_status, error=error,
                                      first_seen_utc=existing['first_seen_utc'] if existing else legacy.now(),
                                      last_checked_utc=legacy.now())
                        if existing is not None:
                            rows[rows.index(existing)] = result
                        else:
                            rows.append(result)
                        save_rows(out, rows)
                        if not error:
                            good.add(product['product_id'])
                        if retries:
                            retries.pop(0)
                        else:
                            pending.pop(0)
                            if not pending:
                                advance_category(state)
                        processed_this_run += 1
                        save()
                        if state.get('consecutive_product_denials', 0) >= 3:
                            raise ReviewRequired('Three consecutive product HTTP 403 responses; access review required')
                        status('running')
                        print(json.dumps(dict(product_id=product['product_id'], favorite=raw,
                                              collected=len(rows), next_page=state['next_category_page'])), flush=True)
                        if args.max_products and processed_this_run >= args.max_products:
                            status('sample_completed')
                            return 0
                        time.sleep(control_state.delay_seconds)
                    status('completed_with_gaps' if state.get('unavailable_products') else 'completed')
                    return 0
                finally:
                    page.close()
        except ReviewRequired as exc:
            status('blocked_review_required', str(exc))
            print(str(exc), flush=True)
            return 78
        except KeyboardInterrupt:
            status('stopped')
            return 0
        except Exception as exc:
            if STOP_REQUESTED:
                status('stopped')
                return 0
            status('retryable_error', f'{type(exc).__name__}: {exc}')
            raise


if __name__ == '__main__':
    def stop_requested(signum, frame):
        global STOP_REQUESTED
        STOP_REQUESTED = True
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_requested)
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=legacy.OUT)
    parser.add_argument('--cdp', default='http://127.0.0.1:9223')
    parser.add_argument('--daily-limit', type=int, default=10000)
    parser.add_argument('--delay', type=float, default=3)
    parser.add_argument('--max-products', type=int, default=0)
    raise SystemExit(run(parser.parse_args()))
