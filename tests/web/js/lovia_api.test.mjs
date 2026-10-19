// window.lovia (lovia.js): its members and events are a public contract —
// renaming or dropping one breaks deployers' scripts, so they are pinned here.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { EVENTS, configureLovia, emitLovia, lovia } from '../../../lovia/web/static/js/lovia.js';

test('the API surface is exactly the documented one', () => {
  assert.deepEqual(Object.keys(lovia).sort(), [
    'api',
    'getRenderer',
    'newChat',
    'on',
    'openSession',
    'ready',
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
