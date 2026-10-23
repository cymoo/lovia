// Registry (registry.js): the stand-in-until-disposed rule behind every
// window.lovia register* call.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Registry } from '../../../lovia/web/static/js/registry.js';

test('a registration stands in for the one it replaced until disposed', () => {
  const reg = new Registry();
  const offA = reg.add('k', 'a');
  const offB = reg.add('k', 'b');
  assert.equal(reg.get('k'), 'b');
  offB();
  assert.equal(reg.get('k'), 'a');
  offA();
  assert.equal(reg.get('k'), undefined);
  assert.equal(reg.has('k'), false);
});

test('disposals in any order leave whatever still stands on top', () => {
  const reg = new Registry();
  let changes = 0;
  const offA = reg.add('k', 'a', () => changes++);
  const offB = reg.add('k', 'b', () => changes++);
  const offC = reg.add('k', 'c', () => changes++);
  offB(); // not the current one: nothing to redraw
  assert.equal(reg.get('k'), 'c');
  assert.equal(changes, 0);
  offC(); // B is gone too, so A comes back, not B
  assert.equal(reg.get('k'), 'a');
  assert.equal(changes, 1);
  offA();
  offA(); // a second call does nothing
  assert.equal(changes, 2);
  assert.equal(reg.size, 0);
});

test('a stale disposer leaves a later registration under the same key alone', () => {
  const reg = new Registry();
  const off = reg.add('k', 'old');
  off();
  reg.add('k', 'new');
  off();
  assert.equal(reg.get('k'), 'new');
});

test('entries: current values, keys in first-registration order', () => {
  const reg = new Registry();
  reg.add('x', 1);
  reg.add('y', 2);
  reg.add('x', 3);
  assert.deepEqual([...reg.entries()], [['x', 3], ['y', 2]]);
  assert.equal(reg.size, 2);
});
