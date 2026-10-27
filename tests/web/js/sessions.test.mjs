// Exercise the real session polling code with API, storage and DOM boundaries
// injected; no browser or network is needed to reproduce history failures.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { setImmediate } from 'node:timers/promises';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';
import { createRunNotices } from '../../../lovia/web/static/js/run-notices.js';

const source = readFileSync(new URL('../../../lovia/web/static/js/sessions.js', import.meta.url), 'utf8')
  .replace(/^import .*;$/gm, '')
  .replace(/^export /gm, '');
const live = (run_id, session_id = run_id) => ({ run_id, session_id });
const result = (id, status = 'completed') => [{ id, session_id: id, status }];

function fixture(t) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const shown = [];
  const history = new Map();
  const requests = [];
  const storage = new Map([['lovia-runs-seen', '10']]);
  const store = { sessions: [], activeRuns: new Set(), runsBySession: new Map() };
  const state = { runs: [] };
  const context = {
    document: { title: 'Lovia', hidden: false, getElementById: () => null },
    localStorage: {
      getItem: (key) => storage.get(key),
      setItem: (key, value) => storage.set(key, value),
    },
    store, createRunNotices, console, setTimeout, clearTimeout,
    icon: () => '',
    t: (key, params) => ({ key, params }),
    toast: (message) => shown.push(message),
    playCompletionSound: () => {},
    api: {
      listSessions: async () => [],
      listRuns: async () => state.runs,
      runHistory: async ({ run_id }) => {
        requests.push(run_id);
        const answer = history.get(run_id);
        if (answer instanceof Error) throw answer;
        return answer ?? [];
      },
    },
  };
  runInNewContext(`${source}\nglobalThis.poll = loadSessions;`, context);
  return {
    shown, history, requests, storage, store,
    tick: () => t.mock.timers.tick(1000),
    async poll(runs = []) {
      state.runs = runs;
      await context.poll();
      await setImmediate(); // settle detached history requests, including finally
    },
  };
}

test('failed history is retried after another completion without skipping its catch-up watermark', async (t) => {
  const f = fixture(t);
  await f.poll([live('a'), live('b')]);
  f.history.set('a', new Error('temporary outage'));
  await f.poll([live('b')]);
  assert.equal(f.store.activeRuns.has('a'), false);
  assert.equal(f.store.runsBySession.has('a'), false);

  f.history.set('b', result('b'));
  await f.poll();
  f.tick();
  assert.deepEqual(f.shown.map((m) => m.key), ['toast.runFinished']);
  assert.equal(f.storage.get('lovia-runs-seen'), '10');

  f.history.set('a', result('a', 'failed'));
  await f.poll();
  f.tick();
  assert.deepEqual(f.shown.map((m) => m.key), ['toast.runFinished', 'toast.runFailed']);
  assert.notEqual(f.storage.get('lovia-runs-seen'), '10');
  await f.poll();
  assert.deepEqual(f.requests, ['a', 'a', 'b', 'a']);
});

test('empty or still-running history is retried until a terminal outcome is available', async (t) => {
  const f = fixture(t);
  await f.poll([live('a')]);
  await f.poll();
  f.history.set('a', result('a', 'running'));
  await f.poll();
  assert.equal(f.storage.get('lovia-runs-seen'), '10');
  f.history.set('a', result('a'));
  await f.poll();
  f.tick();
  assert.deepEqual(f.shown.map((m) => m.key), ['toast.runFinished']);
  await f.poll();
  assert.deepEqual(f.requests, ['a', 'a', 'a']);
});

test('overlapping refreshes share a pending lookup and retain the old run ID when its chat starts again', async (t) => {
  const f = fixture(t);
  let finish;
  f.history.set('a', new Promise((resolve) => { finish = resolve; }));
  await f.poll([live('a', 'chat')]);
  await f.poll();
  await f.poll([live('b', 'chat')]);
  assert.deepEqual(f.requests, ['a']);
  finish(result('a'));
  await setImmediate();
  f.tick();
  assert.deepEqual(f.shown.map((m) => m.key), ['toast.runFinished']);

  f.history.set('b', result('b', 'failed'));
  await f.poll();
  f.tick();
  assert.deepEqual(f.requests, ['a', 'b']);
  assert.deepEqual(f.shown.map((m) => m.key), ['toast.runFinished', 'toast.runFailed']);
});

test('cancelled and interrupted outcomes stop retrying without a completion notice', async (t) => {
  const f = fixture(t);
  await f.poll([live('a'), live('b')]);
  f.history.set('a', result('a', 'cancelled'));
  f.history.set('b', result('b', 'interrupted'));
  await f.poll();
  await f.poll();
  f.tick();
  assert.deepEqual(f.requests, ['a', 'b']);
  assert.deepEqual(f.shown, []);
});
