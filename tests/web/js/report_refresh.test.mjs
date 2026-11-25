// A task's report kept in a stopped chat (the `session_appended` event) is
// drawn even when it lands while the stopped run's stream is still on screen:
// sessions.js's event stream, with EventSource, API and store injected.
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

function fixture() {
  const fetched = [];
  const listeners = new Map();
  const handlers = {};
  class FakeEventSource {
    static CLOSED = 2;
    addEventListener(name, fn) { handlers[name] = fn; }
  }
  const store = {
    sessionId: 'chat', streaming: false,
    sessions: [], activeRuns: new Set(), runsBySession: new Map(), shownRuns: new Set(),
    emit: () => {},
    on: (name, fn) => listeners.set(name, fn),
  };
  const context = {
    document: { title: 'Lovia', hidden: false, getElementById: () => null },
    localStorage: { getItem: () => null, setItem: () => {} },
    store, createRunNotices, createAttention, console, setTimeout, clearTimeout,
    EventSource: FakeEventSource,
    icon: () => '',
    currentView: () => null,
    canAddWorkspace: () => false,
    renderOrigin: () => {},
    t: (key) => key,
    toast: () => {},
    playCompletionSound: () => {},
    api: {
      eventsUrl: () => '/api/events',
      listSessions: async () => [],
      listRuns: async () => [],
      runHistory: async () => [],
      getSession: async (id) => {
        fetched.push(id);
        return { id, entries: [] };
      },
    },
  };
  runInNewContext(`${source}\ninitEventStream();`, context);
  const appended = (id) => handlers.session_appended({ data: JSON.stringify({ session_id: id }) });
  const runEnd = (sessionId) => listeners.get('run-end')({ sessionId });
  return { store, fetched, appended, runEnd };
}

test('a report for the idle open chat is drawn at once', async () => {
  const f = fixture();
  f.appended('chat');
  await setImmediate();
  assert.deepEqual(f.fetched, ['chat']);
});

test('one that lands mid-stream is drawn when the stream ends', async () => {
  const f = fixture();
  f.store.streaming = true;
  f.appended('chat');
  await setImmediate();
  assert.deepEqual(f.fetched, []); // refreshTranscript skips a streaming chat

  f.store.streaming = false;
  f.runEnd('chat');
  await setImmediate();
  assert.deepEqual(f.fetched, ['chat']);

  f.runEnd('chat'); // later runs' ends don't reload again
  await setImmediate();
  assert.deepEqual(f.fetched, ['chat']);
});

test('another chat\'s report reloads nothing', async () => {
  const f = fixture();
  f.store.streaming = true;
  f.appended('other');
  f.store.streaming = false;
  f.runEnd('chat');
  await setImmediate();
  assert.deepEqual(f.fetched, []);
});
