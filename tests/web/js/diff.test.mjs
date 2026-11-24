// Line diffs (diff.js): what an edit card shows as kept, removed and added.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { lineDiff } from '../../../lovia/web/static/js/diff.js';

const show = (a, b) => lineDiff(a, b).map(({ sign, text }) => sign + text);

test('lines both sides share show once', () => {
  assert.deepEqual(show('- a', '- a\n- b'), [' - a', '+- b']);
  assert.deepEqual(show('a\nb\nc', 'a\nB\nc'), [' a', '-b', '+B', ' c']);
  assert.deepEqual(show('a\nb\nc\nd\ne', 'a\nc\nx\ne'), [' a', '-b', ' c', '-d', '+x', ' e']);
  // equal lines at both ends are each one line, not counted twice
  assert.deepEqual(show('a\na', 'a\na\na'), [' a', ' a', '+a']);
  assert.deepEqual(show('a', 'a'), [' a']);
});

test('removals come before the additions that replace them', () => {
  assert.deepEqual(show('a\nb', 'x\ny'), ['-a', '-b', '+x', '+y']);
});

test('an empty side is no line, an empty line is one', () => {
  assert.deepEqual(show('a\n', ''), ['-a']);
  assert.deepEqual(show('', 'a'), ['+a']);
  assert.deepEqual(show('', ''), []);
  assert.deepEqual(show('a\n\nb', 'a\nb'), [' a', '-', ' b']);
});

test('a closing newline both sides have is no line; one side’s own shows', () => {
  assert.deepEqual(show('a\n', 'b\n'), ['-a', '+b']);
  assert.deepEqual(show('a', 'a\n'), [' a', '+']);
});

test('a change too long to compare shows as replaced whole', () => {
  const many = (p) => Array.from({ length: 1500 }, (_, i) => `${p}${i}`);
  const rows = show(['h', ...many('a'), 't'].join('\n'), ['h', ...many('b'), 't'].join('\n'));
  assert.equal(rows.length, 3002);
  assert.deepEqual([rows[0], rows[1], rows[1501], rows[3001]], [' h', '-a0', '+b0', ' t']);
});

test('every row of both sides is there, in order', () => {
  let seed = 7;
  const rand = (n) => (seed = (seed * 1103515245 + 12345) % 2147483648) % n;
  for (let k = 0; k < 300; k++) {
    const text = () => Array.from({ length: 1 + rand(8) }, () => 'abc'[rand(3)]).join('\n');
    const [a, b] = [text(), text()];
    const rows = lineDiff(a, b);
    const side = (drop) => rows.filter((r) => r.sign !== drop).map((r) => r.text).join('\n');
    assert.equal(side('+'), a);
    assert.equal(side('-'), b);
  }
});
