'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appSource = fs.readFileSync('./dashboard_static/app.js', 'utf8');
const start = appSource.indexOf('const DASHBOARD_MUTATION_TOKEN_HEADER');
const end = appSource.indexOf('\nfunction normalizeEtsyStatus', start);
assert.ok(start >= 0 && end > start, 'Unable to isolate dashboard fetch wrapper');
const fetchWrapperSource = appSource.slice(start, end);

class TestDOMParser {
  parseFromString(html) {
    const match = String(html).match(
      /<meta\s+name=["']etsy-dashboard-mutation-token["']\s+content=["']([^"']*)["']/i,
    );
    return {
      querySelector(selector) {
        if (selector !== 'meta[name="etsy-dashboard-mutation-token"]' || !match) return null;
        return { content: match[1] };
      },
    };
  }
}

function response(status, data, contentType = 'application/json') {
  const body = contentType === 'application/json' ? JSON.stringify(data) : String(data);
  return new Response(body, { status, headers: { 'content-type': contentType } });
}

function createHarness(nativeFetch) {
  const context = {
    module: { exports: {} },
    URL,
    Headers,
    Request,
    Response,
    DOMParser: TestDOMParser,
    document: {
      querySelector(selector) {
        return selector === 'meta[name="etsy-dashboard-mutation-token"]'
          ? { content: 'stale-token' }
          : null;
      },
    },
    window: {
      location: {
        href: 'http://127.0.0.1:8090/',
        origin: 'http://127.0.0.1:8090',
      },
      fetch: nativeFetch,
    },
  };
  vm.createContext(context);
  vm.runInContext(`${fetchWrapperSource}\nmodule.exports = window.fetch;`, context);
  return context.module.exports;
}

async function testRecoversWithFreshTokenAndReusableBody() {
  const calls = [];
  const nativeFetch = async (input, init = {}) => {
    calls.push({ url: String(input), init });
    if (calls.length === 1) {
      assert.equal(new Headers(init.headers).get('X-Dashboard-Mutation-Token'), 'stale-token');
      assert.equal(init.body, '{"title":"Planner"}');
      return response(403, { detail: 'missing or invalid mutation token' });
    }
    if (calls.length === 2) {
      assert.equal(String(input), '/');
      assert.equal(init.method, 'GET');
      assert.equal(init.cache, 'no-store');
      return response(
        200,
        '<meta name="etsy-dashboard-mutation-token" content="fresh-token">',
        'text/html',
      );
    }
    assert.equal(new Headers(init.headers).get('X-Dashboard-Mutation-Token'), 'fresh-token');
    assert.equal(init.body, '{"title":"Planner"}');
    return response(200, { ok: true });
  };
  const dashboardFetch = createHarness(nativeFetch);
  const originalInit = {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: '{"title":"Planner"}',
  };
  const result = await dashboardFetch('/api/products/410', originalInit);
  assert.equal(result.status, 200);
  assert.equal(calls.length, 3);
  assert.deepEqual(originalInit.headers, { 'Content-Type': 'application/json' });
  assert.equal(originalInit.body, '{"title":"Planner"}');
}

async function testBodyBearingRequestCanBeRetriedWithoutConsumption() {
  const mutationBodies = [];
  const mutationTokens = [];
  const dashboardFetch = createHarness(async (input, init = {}) => {
    if (String(input) === '/') {
      return response(
        200,
        '<meta name="etsy-dashboard-mutation-token" content="fresh-token">',
        'text/html',
      );
    }
    mutationBodies.push(await input.text());
    mutationTokens.push(new Headers(init.headers).get('X-Dashboard-Mutation-Token'));
    return mutationBodies.length === 1
      ? response(403, { detail: 'missing or invalid mutation token' })
      : response(200, { ok: true });
  });
  const originalRequest = new Request('http://127.0.0.1:8090/api/products/410', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: '{"title":"Request body"}',
  });
  const result = await dashboardFetch(originalRequest);
  assert.equal(result.status, 200);
  assert.deepEqual(mutationBodies, ['{"title":"Request body"}', '{"title":"Request body"}']);
  assert.deepEqual(mutationTokens, ['stale-token', 'fresh-token']);
  assert.equal(originalRequest.bodyUsed, false, 'wrapper must not consume the caller Request');
}

async function testExactErrorAndSameOriginGating() {
  for (const detail of ['host/origin rejected', 'Forbidden']) {
    let calls = 0;
    const dashboardFetch = createHarness(async () => {
      calls += 1;
      return response(403, { detail });
    });
    const result = await dashboardFetch('/api/products/410', { method: 'PATCH' });
    assert.equal(result.status, 403);
    assert.equal(calls, 1, `must not refresh for ${detail}`);
  }

  let crossOriginCalls = 0;
  const dashboardFetch = createHarness(async () => {
    crossOriginCalls += 1;
    return response(403, { detail: 'missing or invalid mutation token' });
  });
  await dashboardFetch('https://example.com/api/products/410', { method: 'PATCH' });
  assert.equal(crossOriginCalls, 1, 'must not refresh a cross-origin mutation');
}

async function testRetriesOnlyOnce() {
  let calls = 0;
  const dashboardFetch = createHarness(async (input) => {
    calls += 1;
    if (String(input) === '/') {
      return response(
        200,
        '<meta name="etsy-dashboard-mutation-token" content="fresh-token">',
        'text/html',
      );
    }
    return response(403, { detail: 'missing or invalid mutation token' });
  });
  const result = await dashboardFetch('/api/products/410', { method: 'PATCH' });
  assert.equal(result.status, 403);
  assert.equal(calls, 3, 'one mutation, one refresh, and exactly one mutation retry');
}

async function testConcurrentFailuresShareOneRefresh() {
  let refreshCalls = 0;
  let mutationCalls = 0;
  let releaseRefresh;
  const refreshGate = new Promise(resolve => { releaseRefresh = resolve; });
  const dashboardFetch = createHarness(async (input) => {
    if (String(input) === '/') {
      refreshCalls += 1;
      await refreshGate;
      return response(
        200,
        '<meta name="etsy-dashboard-mutation-token" content="fresh-token">',
        'text/html',
      );
    }
    mutationCalls += 1;
    if (mutationCalls <= 2) {
      return response(403, { detail: 'missing or invalid mutation token' });
    }
    return response(200, { ok: true });
  });
  const first = dashboardFetch('/api/products/410', { method: 'PATCH', body: 'one' });
  const second = dashboardFetch('/api/products/411', { method: 'POST', body: 'two' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(refreshCalls, 1);
  releaseRefresh();
  const results = await Promise.all([first, second]);
  assert.deepEqual(results.map(item => item.status), [200, 200]);
  assert.equal(refreshCalls, 1);
  assert.equal(mutationCalls, 4);
}

(async () => {
  await testRecoversWithFreshTokenAndReusableBody();
  await testBodyBearingRequestCanBeRetriedWithoutConsumption();
  await testExactErrorAndSameOriginGating();
  await testRetriesOnlyOnce();
  await testConcurrentFailuresShareOneRefresh();
  console.log('mutation token refresh tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
