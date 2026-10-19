// window.lovia (lovia.js): its members and events are a public contract —
// renaming or dropping one breaks deployers' scripts, so they are pinned here.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  EVENTS,
  collectContext,
  configureLovia,
  emitLovia,
  lovia,
} from '../../../lovia/web/static/js/lovia.js';
import { configureApi } from '../../../lovia/web/static/js/api.js';

test('the API surface is exactly the documented one', () => {
  assert.deepEqual(Object.keys(lovia).sort(), [
    'api',
    'fetch',
    'getRenderer',
    'newChat',
    'on',
    'openSession',
    'provideContext',
    'ready',
    'refreshMessageActions',
    'registerMessageAction',
    'registerRenderer',
    'registerToolView',
    'send',
    'setDraft',
    'state',
    't',
    'toast',
    'version',
  ]);
  assert.deepEqual([...EVENTS], ['session', 'agent', 'run-start', 'run-end', 'theme']);
  assert.ok(Object.isFrozen(lovia));
});

test('on() delivers events until unsubscribed, and rejects unknown ones', () => {
  const seen = [];
  const off = lovia.on('run-end', (p) => seen.push(p.status));
  emitLovia('run-end', { status: 'done' });
  off();
  emitLovia('run-end', { status: 'error' });
  assert.deepEqual(seen, ['done']);
  assert.throws(() => lovia.on('run-ended', () => {}), /unknown event "run-ended"/);
  assert.throws(() => lovia.on('theme', 'nope'), /expected a listener function/);
});

test('a throwing listener is contained; the others still run', () => {
  const quiet = console.error;
  console.error = () => {};
  try {
    const seen = [];
    const offs = [
      lovia.on('theme', () => {
        throw new Error('boom');
      }),
      lovia.on('theme', (p) => seen.push(p.theme)),
    ];
    emitLovia('theme', { theme: 'dark' });
    assert.deepEqual(seen, ['dark']);
    offs.forEach((off) => off());
  } finally {
    console.error = quiet;
  }
});

test('provideContext: values read at send time; a failing or empty one is left out', () => {
  assert.equal(collectContext(), undefined); // nothing provided: no context
  let kb = 'docs';
  lovia.provideContext('kb', () => kb);
  lovia.provideContext('none', () => undefined);
  const quiet = console.error;
  console.error = () => {};
  try {
    lovia.provideContext('broken', () => {
      throw new Error('boom');
    });
    assert.deepEqual(collectContext(), { kb: 'docs' });
    kb = 'tickets';
    assert.deepEqual(collectContext(), { kb: 'tickets' });
  } finally {
    console.error = quiet;
  }
  assert.throws(() => lovia.provideContext('', () => 1), /expected a key/);
  assert.throws(() => lovia.provideContext('x', 1), /expected a function/);
});

test('provideContext: a "__proto__" key is a key like any other', () => {
  lovia.provideContext('__proto__', () => ({ x: 1 }));
  const quiet = console.error;
  console.error = () => {}; // the earlier test's broken provider
  try {
    const ctx = collectContext();
    assert.ok(Object.hasOwn(ctx, '__proto__'));
    assert.match(JSON.stringify(ctx), /"__proto__":\{"x":1\}/);
  } finally {
    console.error = quiet;
  }
});

test('actions reach the page, normalized', async () => {
  const calls = [];
  configureLovia({
    send: (t) => calls.push(['send', t]),
    setDraft: (t) => calls.push(['setDraft', t]),
    newChat: () => calls.push(['newChat']),
    openSession: async (id) => void calls.push(['openSession', id]),
    toast: (t, o) => calls.push(['toast', t, o.type]),
    state: () => ({ sessionId: 's1', agent: 'bot', theme: 'dark', lang: 'zh', busy: true }),
  });
  lovia.send('  hi  ');
  lovia.send('   '); // nothing to send
  lovia.setDraft('draft');
  lovia.newChat();
  await lovia.openSession('s9');
  lovia.toast('saved');
  lovia.toast('oops', { type: 'error' });
  assert.deepEqual(calls, [
    ['send', 'hi'],
    ['setDraft', 'draft'],
    ['newChat'],
    ['openSession', 's9'],
    ['toast', 'saved', 'info'],
    ['toast', 'oops', 'error'],
  ]);
  const state = lovia.state;
  assert.equal(state.sessionId, 's1');
  assert.equal(state.busy, true);
  assert.ok(Object.isFrozen(state));
});

test('fetch() takes an app path and adds the mount prefix', async () => {
  const requested = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    requested.push([url, init?.method]);
    return new Response('{}');
  };
  try {
    configureApi({ base: '/lovia' });
    await lovia.fetch('/feedback', { method: 'POST' });
    assert.deepEqual(requested, [['/lovia/feedback', 'POST']]);
    assert.throws(() => lovia.fetch('feedback'), /starting with '\/'/);
  } finally {
    configureApi({ base: '' });
    globalThis.fetch = realFetch;
  }
});
