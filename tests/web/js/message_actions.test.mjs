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
  assert.throws(() => registerMessageAction('a', { run }), /`label`/);
  assert.throws(() => registerMessageAction('a', { label: 'x' }), /`run` is required/);
  assert.throws(() => registerMessageAction('a', { label: 'x', run, when: true }), /`when`/);
  assert.throws(() => registerMessageAction('a', { label: 'x', run, onClick: run }), /"onClick"/);
  assert.throws(() => registerMessageAction('a', { label: 'x', run, icon: 1 }), /`icon`/);
});

test('when picks the messages; registration order is button order; null removes', () => {
  registerMessageAction('up', { label: 'Good', run() {}, when: (m) => m.ref !== null });
  registerMessageAction('note', { label: 'Note', run() {} });
  assert.deepEqual(ids(messageActionButtons(() => msg())), ['up', 'note']);
  assert.deepEqual(ids(messageActionButtons(() => msg({ ref: null }))), ['note']);
  registerMessageAction('up', null);
  registerMessageAction('note', null);
  assert.deepEqual(messageActionButtons(() => msg()), []);
});

test('pressed sets aria-pressed; a throwing predicate counts as false', async () => {
  registerMessageAction('star', { label: 'Star', run() {}, pressed: (m) => m.ref === 'r1:0' });
  registerMessageAction('bad', { label: 'Bad', run() {}, when: () => { throw new Error('x'); } });
  const buttons = await quietly(() => messageActionButtons(() => msg()));
  assert.deepEqual(ids(buttons), ['star']);
  assert.equal(buttons[0].attrs['aria-pressed'], 'true');
  assert.equal(buttons[0].attrs['aria-label'], 'Star');
  registerMessageAction('star', null);
  registerMessageAction('bad', null);
});

test('the glyph: a built-in icon, SVG markup, or text (the label by default)', () => {
  registerMessageAction('a', { label: 'Copy', icon: 'copy', run() {} });
  registerMessageAction('b', { label: 'Mine', icon: '<svg viewBox="0 0 1 1"></svg>', run() {} });
  registerMessageAction('c', { label: 'Like', icon: '👍', run() {} });
  registerMessageAction('d', { label: 'Flag', run() {} });
  const [a, b, c, d] = messageActionButtons(() => msg());
  assert.match(a.innerHTML, /^<svg[^>]*width="13"/);
  assert.equal(b.innerHTML, '<svg viewBox="0 0 1 1"></svg>');
  assert.equal(c.textContent, '👍');
  assert.equal(c.innerHTML, '');
  assert.equal(d.textContent, 'Flag');
  for (const id of 'abcd') registerMessageAction(id, null);
});

test('a click runs with the message as it is then, disabled until it settles', async () => {
  const seen = [];
  let release = () => {};
  registerMessageAction('go', {
    label: 'Go',
    run: (m) => {
      seen.push(m.ref);
      return new Promise((r) => { release = r; });
    },
  });
  let ref = null;
  const [btn] = messageActionButtons(() => msg({ ref }));
  ref = 'r9:0'; // the run named the turn after the button was drawn
  const done = btn.click();
  assert.equal(btn.disabled, true);
  release();
  await done;
  assert.equal(btn.disabled, false);
  assert.deepEqual(seen, ['r9:0']);
  registerMessageAction('go', null);
});

test('registering and refreshMessageActions ask the page to redraw', () => {
  let redraws = 0;
  watchMessageActions(() => redraws++);
  registerMessageAction('x', { label: 'X', run() {} });
  refreshMessageActions();
  registerMessageAction('x', null);
  assert.equal(redraws, 3);
});
