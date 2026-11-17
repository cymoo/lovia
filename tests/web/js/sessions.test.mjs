// Exercise the real session polling code with API, storage and DOM boundaries
// injected; no browser or network is needed to reproduce history failures.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { setImmediate } from 'node:timers/promises';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';
import { createRunNotices } from '../../../lovia/web/static/js/run-notices.js';
import { createAttention } from '../../../lovia/web/static/js/attention.js';

const source = readFileSync(new URL('../../../lovia/web/static/js/sessions.js', import.meta.url), 'utf8')
  .replace(/^import .*;$/gm, '')
  .replace(/^export /gm, '');
const live = (run_id, session_id = run_id) => ({ run_id, session_id });
// Shaped like the server's RunRecordInfo (`run_id`, not `id`).
const result = (run_id, status = 'completed', session_id = run_id) => [{ run_id, session_id, status }];

function fixture(t) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const shown = [];
  const notified = [];
  const history = new Map();
  const requests = [];
  const storage = new Map([['lovia-runs-seen', '10']]);
  const store = {
    sessions: [], activeRuns: new Set(), runsBySession: new Map(), shownRuns: new Set(),
    emit: () => {}, // the real store's event hook (no listeners here)
  };
  const state = { runs: [] };
  const document = { title: 'Lovia', hidden: false, getElementById: () => null };
  class FakeNotification {
    constructor(title, opts) { notified.push({ title, ...opts }); }
  }
  const context = {
    document,
    localStorage: {
      getItem: (key) => storage.get(key),
      setItem: (key, value) => storage.set(key, value),
    },
    store, createRunNotices, createAttention, console, setTimeout, clearTimeout,
    Notification: FakeNotification,
    notificationsEnabled: () => true,
    icon: () => '',
    currentView: () => null,
    canAddWorkspace: () => false,
    t: (key, params) => ({ key, params }),
    toast: (message, opts) => shown.push({ ...message, action: opts?.action?.label }),
    playCompletionSound: () => {},
    api: {
      listSessions: async () => state.sessions ?? [],
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
    shown, notified, history, requests, storage, store, document, state,
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

test('the open chat gets no notice for a run this page streamed, but does for one it never showed', async (t) => {
  const f = fixture(t);
  f.store.sessionId = 'chat';
  f.store.shownRuns.add('mine');
  await f.poll([live('mine', 'chat')]);
  f.history.set('mine', result('mine', 'completed', 'chat'));
  await f.poll();
  f.tick();
  assert.deepEqual(f.shown, []);

  await f.poll([live('theirs', 'chat')]);
  f.history.set('theirs', result('theirs', 'completed', 'chat'));
  await f.poll();
  f.tick();
  assert.deepEqual(f.shown.map((m) => m.key), ['toast.runFinished']);
});

const waiting = (run_id, session_id, kind, id) => ({
  run_id, session_id, status: kind === 'approval' ? 'blocked_on_approval' : 'waiting',
  waits_on: { kind, id },
});

test('a run that starts waiting on the user is told once, by kind', async (t) => {
  const f = fixture(t);
  f.state.sessions = [{ id: 'a', title: 'Deploy' }];
  await f.poll([waiting('old', 'z', 'approval', 'c0')]); // seeds: no news
  await f.poll([waiting('r1', 'a', 'approval', 'c1')]);
  // The same card, now with the run suspended on it: the same wait.
  await f.poll([{ ...waiting('r1', 'a', 'approval', 'c1'), status: 'waiting' }]);
  await f.poll([waiting('r2', 'b', 'question', 'q1')]);
  const title = (p) => p?.title?.key ?? p?.title; // t() returns {key, params} here
  assert.deepEqual(
    f.shown.map((m) => [m.key, title(m.params), m.action?.key]),
    [
      ['toast.runWaitsApproval', 'Deploy', 'toast.openChat'],
      ['toast.runWaitsAnswer', 'toast.backgroundRun', 'toast.openChat'],
    ],
  );
});

test('no notice for the chat on screen; a hidden tab gets the badge and a notification', async (t) => {
  const f = fixture(t);
  f.store.sessionId = 'a';
  await f.poll();
  await f.poll([waiting('r1', 'a', 'approval', 'c1')]);
  assert.deepEqual(f.shown, []);

  f.document.hidden = true;
  await f.poll([waiting('r1', 'a', 'approval', 'c1'), waiting('r2', 'a', 'call', 'c2')]);
  assert.deepEqual(f.shown, []);
  assert.equal(f.document.title, '(1) Lovia');
  assert.equal(f.notified.length, 1);
  assert.equal(f.notified[0].tag, 'lovia-wait:a');
});

// Opening a chat by id (a reload's ?session=…) with the server's detail.
function opener(detail) {
  const events = [];
  const urls = [];
  const transcript = { replaceChildren: () => {} };
  const elements = {
    transcript,
    'tmpl-skeleton': { content: { cloneNode: () => ({}) } },
  };
  const store = {
    sessionId: null, sessions: [], activeRuns: new Set(), runsBySession: new Map(), shownRuns: new Set(),
    emit: (name) => events.push(name),
    syncURL: (id) => urls.push(id),
  };
  const context = {
    document: { title: 'Lovia', hidden: false, getElementById: (id) => elements[id] ?? null },
    localStorage: { getItem: () => null, setItem: () => {} },
    store, createRunNotices, createAttention, console, setTimeout, clearTimeout,
    icon: () => '',
    t: (key) => key,
    toast: () => {},
    playCompletionSound: () => {},
    closeView: () => {},
    closeSidebar: () => {},
    currentView: () => null,
    canAddWorkspace: () => false,
    api: { getSession: async () => detail },
  };
  runInNewContext(`${source}\nglobalThis.open = switchSession;`, context);
  return { store, events, urls, open: context.open };
}

test('a chat id the server has no row for opens a new chat, not one to link', async () => {
  const f = opener({ id: 'stale', exists: false, entries: [], active_run_id: null, workspace_root: null });
  await f.open('stale');
  assert.equal(f.store.sessionId, null);
  assert.deepEqual(f.urls, ['stale', null]);
  assert.ok(f.events.includes('new-chat'));
  assert.ok(!f.events.includes('session-meta'));
});

test('a stored chat opens as itself', async () => {
  const f = opener({ id: 'old', exists: true, entries: [], active_run_id: null, workspace_root: null });
  await f.open('old');
  assert.equal(f.store.sessionId, 'old');
  assert.ok(f.events.includes('session-meta'));
  assert.ok(!f.events.includes('new-chat'));
});
