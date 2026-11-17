// Stopping a chat offers to stop its still-running tasks (sessions.js
// offerToStopTasks), with API, store and toast injected — no browser.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { setImmediate } from 'node:timers/promises';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';
import { createRunNotices } from '../../../lovia/web/static/js/run-notices.js';

const source = readFileSync(new URL('../../../lovia/web/static/js/sessions.js', import.meta.url), 'utf8')
  .replace(/^import .*;$/gm, '')
  .replace(/^export /gm, '');

function fixture(tasks, live, { failing = [] } = {}) {
  const toasts = [];
  const cancelled = [];
  // The cached runs know none of them: the offer must read runs fetched now.
  const store = {
    sessions: [], activeRuns: new Set(), shownRuns: new Set(), emit: () => {},
    runsBySession: new Map(),
  };
  const context = {
    document: { title: 'Lovia', hidden: false, getElementById: () => null },
    localStorage: { getItem: () => null, setItem: () => {} },
    store, createRunNotices, console, setTimeout, clearTimeout,
    icon: () => '',
    currentView: () => null,
    canAddWorkspace: () => false,
    t: (key, params) => ({ key, params }),
    toast: (message, opts) => toasts.push({ message, opts }),
    playCompletionSound: () => {},
    api: {
      listSessions: async ({ parent } = {}) => (parent ? tasks.map((id) => ({ id })) : []),
      listRuns: async () => live.map((id) => ({ run_id: `r-${id}`, session_id: id })),
      runHistory: async () => [],
      cancel: async (sid) => {
        if (failing.includes(sid)) throw new Error('500');
        cancelled.push(sid);
      },
    },
  };
  runInNewContext(`${source}\nglobalThis.offer = offerToStopTasks;`, context);
  return { toasts, cancelled, live, offer: context.offer };
}

test('no notice when none of the chat\'s tasks is running', async () => {
  const f = fixture(['t1', 't2'], []);
  await f.offer('chat');
  assert.deepEqual(f.toasts, []);
});

test('the notice counts the running tasks and its action stops only those', async () => {
  const f = fixture(['t1', 't2', 't3'], ['t1', 't3']);
  await f.offer('chat');
  assert.equal(f.toasts.length, 1);
  const { message, opts } = f.toasts[0];
  // Field by field: objects made inside the vm context aren't deep-equal
  // (another realm's Object.prototype) to literals made here.
  assert.equal(message.key, 'toast.tasksStillRunning');
  assert.equal(message.params.n, 2);
  assert.equal(opts.action.label.key, 'toast.stopTasks');

  f.live.splice(f.live.indexOf('t3'), 1); // finished before the click
  opts.action.run();
  await setImmediate();
  assert.deepEqual(f.cancelled, ['t1']);
  assert.deepEqual(f.toasts.slice(1).map((x) => x.message.key), ['toast.runStopped']);
});

test('a stop that fails says so', async () => {
  const f = fixture(['t1', 't2'], ['t1', 't2'], { failing: ['t2'] });
  await f.offer('chat');
  f.toasts[0].opts.action.run();
  await setImmediate();
  assert.deepEqual(f.cancelled, ['t1']);
  assert.deepEqual(f.toasts.slice(1).map((x) => x.message.key), ['toast.stopFailed']);
});
