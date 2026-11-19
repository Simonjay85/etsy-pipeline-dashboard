import json
import sys
from types import SimpleNamespace
import types
from datetime import datetime, timezone

import pytest

import creativefabrica_vps_survey as worker


def row(pid, error='', favorite='5'):
    return dict(product_id=pid, title='Test', product_url='https://www.creativefabrica.com/product/test/',
                favorite_display='5X', favorite_estimate=favorite, error=error,
        http_status='200', first_seen_utc='before', last_checked_utc='now')


def write_control(path, revision=1, delay_seconds=10, daily_limit=100):
    path.write_text(json.dumps({
        'revision': revision,
        'delay_seconds': delay_seconds,
        'daily_limit': daily_limit,
    }), encoding='utf-8')


class FakePage:
    url = 'https://www.creativefabrica.com/product/test/'

    def close(self):
        pass


class FakeBrowser:
    def __init__(self):
        self.contexts = [SimpleNamespace(new_page=lambda: FakePage())]


def install_fake_playwright(monkeypatch, connect):
    sync_api = types.ModuleType('playwright.sync_api')

    class FakePlaywright:
        chromium = SimpleNamespace(connect_over_cdp=connect)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    sync_api.sync_playwright = FakePlaywright
    playwright = types.ModuleType('playwright')
    playwright.sync_api = sync_api
    monkeypatch.setitem(sys.modules, 'playwright', playwright)
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', sync_api)


def test_control_absence_keeps_args_and_publishes_integer_revision(tmp_path):
    state = worker.ControlState(delay_seconds=10, daily_limit=10000)

    assert state.refresh(tmp_path / worker.CONTROL_FILENAME) is False
    assert state.telemetry() == {
        'effective_config': {'delay_seconds': 10, 'daily_limit': 10000},
        'control_revision': 0,
        'control_error': None,
    }
    assert type(state.telemetry()['control_revision']) is int


@pytest.mark.parametrize(('field', 'value'), [
    ('revision', True),
    ('daily_limit', True),
    ('delay_seconds', True),
    ('delay_seconds', float('nan')),
])
def test_control_rejects_bool_and_non_finite_values(tmp_path, field, value):
    values = {'revision': 1, 'delay_seconds': 10, 'daily_limit': 100}
    values[field] = value
    path = tmp_path / worker.CONTROL_FILENAME
    path.write_text(json.dumps(values), encoding='utf-8')

    with pytest.raises(worker.ControlFileError):
        worker.read_control(path)


@pytest.mark.parametrize('values', [
    {'revision': -1, 'delay_seconds': 10, 'daily_limit': 100},
    {'revision': 1, 'delay_seconds': 9, 'daily_limit': 100},
    {'revision': 1, 'delay_seconds': 301, 'daily_limit': 100},
    {'revision': 1, 'delay_seconds': 10, 'daily_limit': 99},
    {'revision': 1, 'delay_seconds': 10, 'daily_limit': 10001},
])
def test_control_rejects_values_outside_contract(tmp_path, values):
    path = tmp_path / worker.CONTROL_FILENAME
    path.write_text(json.dumps(values), encoding='utf-8')

    with pytest.raises(worker.ControlFileError):
        worker.read_control(path)


def test_control_rejects_symlink_and_oversized_file(tmp_path):
    target = tmp_path / 'real-control.json'
    write_control(target)
    link = tmp_path / worker.CONTROL_FILENAME
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f'symlinks unavailable: {exc}')
    with pytest.raises(worker.ControlFileError, match='symlink'):
        worker.read_control(link)

    oversized = tmp_path / 'oversized-control.json'
    oversized.write_bytes(b'x' * (worker.CONTROL_MAX_BYTES + 1))
    with pytest.raises(worker.ControlFileError, match='oversized'):
        worker.read_control(oversized)


def test_control_revision_changes_apply_and_invalid_updates_keep_last_good(tmp_path):
    path = tmp_path / worker.CONTROL_FILENAME
    state = worker.ControlState(delay_seconds=10, daily_limit=10000)

    write_control(path, revision=1, delay_seconds=11, daily_limit=200)
    assert state.refresh(path) is True
    assert state.telemetry() == {
        'effective_config': {'delay_seconds': 11, 'daily_limit': 200},
        'control_revision': 1,
        'control_error': None,
    }

    write_control(path, revision=0, delay_seconds=10, daily_limit=10000)
    assert state.refresh(path) is True
    assert state.effective_config == {'delay_seconds': 11, 'daily_limit': 200}
    assert state.control_revision == 1
    assert 'rollback' in state.control_error

    write_control(path, revision=1, delay_seconds=12, daily_limit=200)
    assert state.refresh(path) is True
    assert state.effective_config == {'delay_seconds': 11, 'daily_limit': 200}
    assert state.control_revision == 1
    assert 'changed its configuration' in state.control_error

    write_control(path, revision=2, delay_seconds=10, daily_limit=300)
    assert state.refresh(path) is True
    assert state.telemetry() == {
        'effective_config': {'delay_seconds': 10, 'daily_limit': 300},
        'control_revision': 2,
        'control_error': None,
    }


def test_control_error_does_not_change_quota_or_checkpoint_values(tmp_path):
    path = tmp_path / worker.CONTROL_FILENAME
    write_control(path, revision=4, delay_seconds=10, daily_limit=100)
    control = worker.ControlState(delay_seconds=10, daily_limit=10000)
    control.refresh(path)
    checkpoint = {
        'budget_date': '2026-09-14',
        'requests_today': 100,
        'next_category_page': 62,
        'processed_ids': ['one', 'two'],
    }
    before = json.loads(json.dumps(checkpoint))

    write_control(path, revision=3, delay_seconds=20, daily_limit=300)
    control.refresh(path)

    assert checkpoint == before
    assert control.effective_config == {'delay_seconds': 10, 'daily_limit': 100}
    assert control.control_revision == 4
    assert control.control_error


def test_absent_after_valid_control_retains_last_known_good(tmp_path):
    path = tmp_path / worker.CONTROL_FILENAME
    state = worker.ControlState(delay_seconds=10, daily_limit=10000)
    write_control(path, revision=1, delay_seconds=20, daily_limit=500)
    state.refresh(path)
    path.unlink()

    assert state.refresh(path) is False
    assert state.effective_config == {'delay_seconds': 20, 'daily_limit': 500}
    assert state.control_revision == 1
    assert state.control_error is None


def test_startup_status_contains_control_telemetry_before_browser_connect(tmp_path, monkeypatch):
    worker.save_rows(tmp_path, [row('1')])
    (tmp_path / worker.legacy.STATE.name).write_text(json.dumps({
        'completed': True,
        'pending_products': [],
        'next_category_page': 1,
    }), encoding='utf-8')
    control_path = tmp_path / worker.CONTROL_FILENAME
    write_control(control_path, revision=7, delay_seconds=11, daily_limit=500)
    status_path = tmp_path / 'vps_status.json'

    def connect(_cdp):
        startup = json.loads(status_path.read_text(encoding='utf-8'))
        assert startup['status'] == 'starting'
        assert startup['effective_config'] == {'delay_seconds': 11, 'daily_limit': 500}
        assert startup['control_revision'] == 7
        assert type(startup['control_revision']) is int
        return FakeBrowser()

    install_fake_playwright(monkeypatch, connect)
    args = SimpleNamespace(output=tmp_path, cdp='http://127.0.0.1:9223',
                           delay=10, daily_limit=10000, max_products=0)

    assert worker.run(args) == 0


def test_budget_wait_hot_limit_resumes_without_quota_reset(tmp_path, monkeypatch):
    worker.save_rows(tmp_path, [row('1', error='previous transient failure', favorite='')])
    today = datetime.now(timezone.utc).date().isoformat()
    (tmp_path / worker.legacy.STATE.name).write_text(json.dumps({
        'completed': True,
        'pending_products': [],
        'next_category_page': 1,
        'budget_date': today,
        'requests_today': 100,
        'processed_ids': [],
    }), encoding='utf-8')
    control_path = tmp_path / worker.CONTROL_FILENAME
    write_control(control_path, revision=1, delay_seconds=10, daily_limit=100)
    sleep_calls = []

    def sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) == 1:
            write_control(control_path, revision=2, delay_seconds=10, daily_limit=200)

    def connect(_cdp):
        return FakeBrowser()

    install_fake_playwright(monkeypatch, connect)
    monkeypatch.setattr(worker.time, 'sleep', sleep)
    monkeypatch.setattr(worker, 'load_page',
                        lambda _page, _url: ('<span id="favorite-count">7X</span>', 200))
    args = SimpleNamespace(output=tmp_path, cdp='http://127.0.0.1:9223',
                           delay=10, daily_limit=100, max_products=0)

    assert worker.run(args) == 0

    checkpoint = json.loads((tmp_path / worker.legacy.STATE.name).read_text(encoding='utf-8'))
    status = json.loads((tmp_path / 'vps_status.json').read_text(encoding='utf-8'))
    assert sleep_calls[0] == 30
    assert checkpoint['requests_today'] == 101
    assert checkpoint['budget_date'] == today
    assert status['effective_config'] == {'delay_seconds': 10, 'daily_limit': 200}
    assert status['control_revision'] == 2
    assert status['control_error'] is None


def test_recovery_retries_errors_and_skips_committed_rows():
    rows = [row('1'), row('2', error='timeout', favorite='')]
    good, pending = worker.reconcile_pending(rows, {'pending_products': [row('1'), row('3')]})
    assert good == {'1'}
    assert [p['product_id'] for p in pending] == ['2', '3']


def test_access_denied_gap_is_not_retried_after_restart():
    good, pending = worker.reconcile_pending([row('1'), row('2', 'HTTP 403', '')],
        {'pending_products': [row('2')], 'unavailable_products': {'2': {'http_status': 403}}})
    assert good == {'1'}
    assert pending == []


def test_atomic_csv_roundtrip_and_duplicate_guard(tmp_path):
    worker.save_rows(tmp_path, [row('1'), row('2', favorite='0')])
    assert len(worker.read_rows(tmp_path)) == 2
    assert worker.valid(worker.read_rows(tmp_path)[1])
    worker.save_rows(tmp_path, [row('1'), row('1')])
    with pytest.raises(worker.ReviewRequired, match='Duplicate'):
        worker.read_rows(tmp_path)


@pytest.mark.parametrize('url', ['http://www.creativefabrica.com/product/a/',
    'https://evil.test/product/a/', 'https://www.creativefabrica.com/account/',
    'https://www.creativefabrica.com@evil.test/product/a/'])
def test_only_expected_public_routes(url):
    with pytest.raises(worker.ReviewRequired):
        worker.checked_url(url)


@pytest.mark.parametrize('code', [401, 403, 429, 404])
def test_access_failure_does_not_become_completion(code):
    page = SimpleNamespace(goto=lambda *a, **kw: SimpleNamespace(status=code),
                           url='https://www.creativefabrica.com/product/test/')
    with pytest.raises(worker.ReviewRequired):
        worker.load_page(page, page.url)


def test_upstream_500_is_retryable():
    page = SimpleNamespace(goto=lambda *a, **kw: SimpleNamespace(status=500),
                           url='https://www.creativefabrica.com/product/test/')
    with pytest.raises(RuntimeError, match='Transient upstream'):
        worker.load_page(page, page.url)


def test_report_keeps_missing_separate_from_zero(tmp_path):
    worker.write_report(tmp_path, [row('1', favorite='0'), row('2', 'denied', '')],
        {'status': 'running', 'updated_at': 'now', 'next_category_page': 62})
    report = (tmp_path / worker.legacy.REPORT.name).read_text()
    assert 'Verified favorite values: 1' in report
    assert 'Missing values (not zero): 1' in report
    assert 'Median of parsed values: 0.0' in report


def test_checkpoint_atomic_write(tmp_path):
    target = tmp_path / 'state.json'
    worker.atomic_json(target, {'next_category_page': 62})
    assert json.loads(target.read_text())['next_category_page'] == 62
    assert not target.with_suffix('.json.tmp').exists()


def test_completion_requires_explicit_catalog_end():
    assert worker.category_terminal('Showing 185,185-185,199 of 185,199 results')
    assert not worker.category_terminal('Showing 1-36 of 185,199 results')
    state = {'next_category_page': 62, 'last_page_has_next': False}
    with pytest.raises(worker.ReviewRequired):
        worker.advance_category(state)
    assert not state.get('completed')
    state['last_page_terminal'] = True
    worker.advance_category(state)
    assert state['completed']
