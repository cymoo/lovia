// Slash commands (commands.js): registration rules, what is on offer, which
// message names a command, and that a failing one is a toast, not a throw.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  findCommand,
  listCommands,
  registerCommand,
  runCommand,
} from '../../../lovia/web/static/js/commands.js';

const names = () => listCommands().map((c) => c.name);

test('a name is typed after a slash: lower-case, digits and dashes', () => {
  const ok = { description: 'd', run() {} };
  for (const bad of ['', 'Rename', '1st', 'a b', 'a_b', '/x']) {
    assert.throws(() => registerCommand(bad, ok), TypeError, bad);
  }
  assert.throws(() => registerCommand('x', { run() {} }), /description/);
  assert.throws(() => registerCommand('x', { description: 'd' }), /run/);
  assert.throws(() => registerCommand('x', { ...ok, label: 'x' }), /unknown field/);
  assert.throws(() => registerCommand('x', { ...ok, argument: 1 }), /argument/);
});

test('offered in registration order, unless `when` says no — or throws', () => {
  let on = false;
  const undo = [
    registerCommand('one', { description: '1', run() {} }),
    registerCommand('two', { description: '2', when: () => on, run() {} }),
    registerCommand('three', { description: '3', argument: 'What', run() {} }),
    registerCommand('four', { description: '4', when: () => { throw new Error('x'); }, run() {} }),
  ];
  const quiet = console.error;
  console.error = () => {};
  try {
    assert.deepEqual(names(), ['one', 'three']);
    on = true;
    assert.deepEqual(names(), ['one', 'two', 'three']);
    assert.equal(listCommands()[2].argument, 'What');
  } finally {
    console.error = quiet;
    undo.forEach((u) => u());
  }
  assert.deepEqual(names(), []);
});

test('a message names a command only as `/name` at its start', () => {
  const undo = registerCommand('rename', { description: 'd', run() {} });
  const off = registerCommand('hidden', { description: 'd', when: () => false, run() {} });
  try {
    assert.deepEqual(findCommand('/rename'), { name: 'rename', args: '' });
    assert.deepEqual(findCommand('/rename  New  title '), { name: 'rename', args: 'New  title' });
    assert.deepEqual(findCommand('/rename\nsecond line'), { name: 'rename', args: 'second line' });
    // Not a command: it goes to the model, where it may name a skill.
    assert.equal(findCommand('/review the diff'), null);
    assert.equal(findCommand('/renamed'), null);
    assert.equal(findCommand('please /rename'), null);
    assert.equal(findCommand('/src/app.py'), null);
    assert.equal(findCommand('/hidden'), null); // not on offer now
  } finally {
    undo();
    off();
  }
});

test('registering a name again replaces it until the newer one is undone', async () => {
  const ran = [];
  const first = registerCommand('go', { description: 'first', run: () => void ran.push('first') });
  const second = registerCommand('go', { description: 'second', run: () => void ran.push('second') });
  assert.equal(listCommands()[0].description, 'second');
  await runCommand('go', '', null);
  second();
  await runCommand('go', '', null);
  first();
  assert.deepEqual(ran, ['second', 'first']);
});

test('run gets the argument text and the chat; a failure does not throw', async () => {
  let got = null;
  const undo = registerCommand('note', { description: 'd', run: (ctx) => { got = ctx; } });
  const bad = registerCommand('boom', { description: 'd', run: async () => { throw new Error('no'); } });
  const quiet = console.error;
  console.error = () => {};
  // toast() reaches for the page; a failed command must get that far.
  const el = () => ({
    classList: { add() {}, remove() {} },
    setAttribute() {},
    append() {},
    appendChild() {},
    addEventListener() {},
    remove() {},
    style: {},
  });
  globalThis.document = /** @type {any} */ ({
    getElementById: () => null,
    createElement: el,
    body: el(),
  });
  globalThis.requestAnimationFrame = /** @type {any} */ (() => 0);
  try {
    await runCommand('note', 'hello', 's1');
    assert.deepEqual({ ...got }, { args: 'hello', sessionId: 's1' });
    await assert.doesNotReject(runCommand('boom', '', null));
    await runCommand('absent', '', null); // nothing to run: nothing happens
  } finally {
    console.error = quiet;
    undo();
    bad();
  }
});
