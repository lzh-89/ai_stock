import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/t1-candidate-review.js', import.meta.url), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const archive = {total:500, archiveVersion:'v1', rounds:[{round:1, planned:80, requested:80, available:78, eligible:75}]};
const next = {job:{status:'complete', target_trade_date:'2026-10-12', resolved_count:500, expected_count:500, updated_at:'2026-10-12 15:10'}, coveragePct:100, groups:[]};
const ok = body => ({ok:true, json:async () => body});
const oldBackend = () => ({ok:false, json:async () => ({error:'invalid run id'})});

function setup(fetcher) {
  const elements = new Map(), events = new Map(), timers = new Map();
  let timerId = 0;
  const element = id => {
    if (!elements.has(id)) elements.set(id, {textContent:'', innerHTML:'', hidden:false, disabled:false, open:false, value:'', listeners:{}, addEventListener(type, fn) { this.listeners[type] = fn; }});
    return elements.get(id);
  };
  vm.runInNewContext(source, {
    document:{getElementById:element}, window:{addEventListener:(type, fn) => events.set(type, fn)},
    fetch:fetcher, AbortController, URLSearchParams, TypeError,
    setTimeout:(fn, ms) => { const id = ++timerId; timers.set(id, {fn, ms}); return id; },
    clearTimeout:id => timers.delete(id),
  });
  return {element, timers, select:runId => events.get('t1-review-run')({detail:{runId}})};
}

test('archive failure does not block next-day success', async () => {
  const app = setup(async url => url.endsWith('archive-summary') ? oldBackend() : ok(next));
  app.select(92); await flush();
  assert.match(app.element('archiveMeta').textContent, /请重启后端/);
  assert.equal(app.element('archiveReload').hidden, false);
  assert.equal(app.element('archiveDetails').hidden, true);
  assert.equal(app.element('nextDayMeta').textContent, '已完整');
});

test('next-day failure does not block archive success', async () => {
  const app = setup(async url => url.endsWith('archive-summary') ? ok(archive) : oldBackend());
  app.select(92); await flush();
  assert.match(app.element('archiveMeta').textContent, /500/);
  assert.match(app.element('archiveFunnel').innerHTML, /通过门禁/);
  assert.match(app.element('nextDayMeta').textContent, /加载失败/);
  assert.equal(app.element('nextDayReload').hidden, false);
});

test('both errors leave loading state and independent retries recover', async () => {
  let failing = true;
  const calls = [];
  const app = setup(async url => { calls.push(url); return failing ? oldBackend() : ok(url.endsWith('archive-summary') ? archive : next); });
  app.select(88); await flush();
  assert.match(app.element('archiveMeta').textContent, /加载失败/);
  assert.match(app.element('nextDayMeta').textContent, /加载失败/);
  failing = false;
  await app.element('archiveReload').listeners.click(); await flush();
  assert.match(app.element('archiveMeta').textContent, /500/);
  assert.equal(calls.length, 3);
  await app.element('nextDayReload').listeners.click(); await flush();
  assert.equal(app.element('nextDayMeta').textContent, '已完整');
  assert.equal(app.element('nextDayReload').hidden, true);
});

test('legacy archives do not request candidates or fabricate next-day data', async () => {
  const calls = [];
  const app = setup(async url => { calls.push(url); return ok({legacy:true}); });
  app.element('archiveDetails').open = true;
  app.select(1); await flush();
  assert.equal(calls.length, 2);
  assert.equal(app.element('archiveDetails').hidden, true);
  assert.equal(app.element('nextDayMetrics').innerHTML, '');
  assert.equal(app.element('nextDayRetry').disabled, true);
});

test('slow old Run responses cannot overwrite a newer Run', async () => {
  const pending = [];
  const app = setup(url => url.includes('/1/') ? new Promise(resolve => pending.push(resolve)) : Promise.resolve(ok(url.endsWith('archive-summary') ? archive : next)));
  app.select(1); app.select(2); await flush();
  pending.forEach(resolve => resolve(ok({legacy:true}))); await flush();
  assert.match(app.element('archiveMeta').textContent, /500/);
  assert.equal(app.element('nextDayMeta').textContent, '已完整');
});

test('hanging requests time out in both sections', async () => {
  const app = setup((url, {signal}) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name:'AbortError'})))));
  app.select(92);
  [...app.timers.values()].filter(t => t.ms === 15000).forEach(t => t.fn()); await flush();
  assert.match(app.element('archiveMeta').textContent, /请求超时/);
  assert.match(app.element('nextDayMeta').textContent, /请求超时/);
  assert.equal(app.timers.size, 0);
});

test('invalid Run selection makes no requests and clears state', async () => {
  let calls = 0;
  const app = setup(async () => { calls++; return ok({}); });
  app.select('invalid'); await flush();
  assert.equal(calls, 0);
  assert.equal(app.element('nextDayMeta').textContent, '未选择 Run');
});
