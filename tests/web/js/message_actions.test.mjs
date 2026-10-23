// Message actions (message-actions.js): registration rules, the buttons one
// message gets (when / pressed / glyph), clicks, and redraw notifications.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  messageActionButtons,
  refreshMessageActions,
  registerMessageAction,
  watchMessageActions,
} from '../../../lovia/web/static/js/message-actions.js';

// Just enough DOM for a button: attributes, a dataset, a click listener.
globalThis.document = /** @type {any} */ ({
  createElement: () => ({
    dataset: {},
    classList: { names: [], add(n) { this.names.push(n); } },
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(type, fn) { this.click = fn; },
    textContent: '',
    innerHTML: '',
  }),
});

const msg = (over = {}) => ({ sessionId: 's1', ref: 'r1:0', role: 'assistant', text: 'hi', ...over });
const ids = (buttons) => buttons.map((b) => b.dataset.action);
const quietly = async (fn) => {
  const error = console.error;
  console.error = () => {};
  try {
    return await fn();
  } finally {
    console.error = error;
  }
};

test('an action needs an id, a label and run; unknown fields are refused', () => {
  const run = () => {};
  assert.throws(() => registerMessageAction('', { label: 'x', run }), /expected an id/);
  assert.throws(() => registerMessageAction('a', null), /expected an action/);
  assert.throws(() => registerMessageAction('a', { run }), /`label`/);
  assert.throws(() => registerMessageAction('a', { label: 'x' }), /`run` is required/);
  assert.throws(() => registerMessageAction('a', { label: 'x', run, when: true }), /`when`/);
  assert.throws(() => registerMessageAction('a', { label: 'x', run, onClick: run }), /"onClick"/);
  assert.throws(() => registerMessageAction('a', { label: 'x', run, icon: 1 }), /`icon`/);
});

test('when picks the messages; registration order is button order; disposing removes', () => {
  const offUp = registerMessageAction('up', { label: 'Good', run() {}, when: (m) => m.ref !== null });
  const offNote = registerMessageAction('note', { label: 'Note', run() {} });
  assert.deepEqual(ids(messageActionButtons(() => msg())), ['up', 'note']);
  assert.deepEqual(ids(messageActionButtons(() => msg({ ref: null }))), ['note']);
  offUp();
  offNote();
  offNote(); // a second call does nothing
  assert.deepEqual(messageActionButtons(() => msg()), []);
});

test('disposing a replacement brings back the action it replaced', () => {
  const offFirst = registerMessageAction('rate', { label: 'Rate', run() {} });
  const offSecond = registerMessageAction('rate', { label: 'Rate v2', run() {} });
  const label = () => messageActionButtons(() => msg())[0]?.attrs['aria-label'];
  assert.equal(label(), 'Rate v2');
  offSecond();
  assert.equal(label(), 'Rate');
  offFirst();
  assert.equal(label(), undefined);
});

test('pressed sets aria-pressed; a throwing predicate counts as false', async () => {
  const offs = [
    registerMessageAction('star', { label: 'Star', run() {}, pressed: (m) => m.ref === 'r1:0' }),
    registerMessageAction('bad', { label: 'Bad', run() {}, when: () => { throw new Error('x'); } }),
  ];
  const buttons = await quietly(() => messageActionButtons(() => msg()));
  assert.deepEqual(ids(buttons), ['star']);
  assert.equal(buttons[0].attrs['aria-pressed'], 'true');
  assert.equal(buttons[0].attrs['aria-label'], 'Star');
  offs.forEach((off) => off());
});

test('the glyph: a built-in icon, SVG markup, or text (the label by default)', () => {
  const offs = [
    registerMessageAction('a', { label: 'Copy', icon: 'copy', run() {} }),
    registerMessageAction('b', { label: 'Mine', icon: '<svg viewBox="0 0 1 1"></svg>', run() {} }),
    registerMessageAction('c', { label: 'Like', icon: '👍', run() {} }),
    registerMessageAction('d', { label: 'Flag', run() {} }),
  ];
  const [a, b, c, d] = messageActionButtons(() => msg());
  assert.match(a.innerHTML, /^<svg[^>]*width="13"/);
  assert.equal(b.innerHTML, '<svg viewBox="0 0 1 1"></svg>');
  assert.equal(c.textContent, '👍');
  assert.equal(c.innerHTML, '');
  assert.equal(d.textContent, 'Flag');
  offs.forEach((off) => off());
});

test('a click runs with the message as it is then, once until it settles', async () => {
  const seen = [];
  let release = () => {};
  const off = registerMessageAction('go', {
    label: 'Go',
    run: (m) => {
      seen.push(m.ref);
      return new Promise((r) => { release = r; });
    },
  });
  let redraws = 0;
  watchMessageActions(() => redraws++);
  const turn = {};
  let ref = null;
  const [btn] = messageActionButtons(() => msg({ ref }), turn);
  ref = 'r9:0'; // the run named the turn after the button was drawn
  const done = btn.click();
  assert.equal(btn.disabled, true);
  // Redrawn while it runs (a refs frame, a refresh): still running, once.
  const [again] = messageActionButtons(() => msg({ ref }), turn);
  assert.equal(again.disabled, true);
  await again.click();
  assert.deepEqual(seen, ['r9:0']);
  // Another message's button isn't held up.
  assert.equal(messageActionButtons(() => msg(), {})[0].disabled, false);
  const before = redraws;
  release();
  await done;
  assert.equal(redraws, before + 1); // settled: drawn again, enabled
  assert.equal(messageActionButtons(() => msg({ ref }), turn)[0].disabled, false);
  off();
});

test('registering, disposing and refreshMessageActions ask the page to redraw', () => {
  let redraws = 0;
  watchMessageActions(() => redraws++);
  const off = registerMessageAction('x', { label: 'X', run() {} });
  refreshMessageActions();
  off();
  assert.equal(redraws, 3);
});
