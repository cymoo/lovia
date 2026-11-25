// A command's output as lines to read (log-text.js).
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createLogText, plainText } from '../../../lovia/web/static/js/log-text.js';

/** Everything `chunks` shows: the completed lines, then the unfinished one. */
function show(chunks, opts) {
  const log = createLogText(opts);
  const lines = [];
  let tail = '';
  for (const chunk of chunks) {
    const out = log.feed(chunk);
    lines.push(...out.lines);
    tail = out.tail;
  }
  return [...lines, tail];
}

/** The same text fed whole, and cut at every place into two. */
function everyCut(text, want, opts) {
  assert.deepEqual(show([text], opts), want);
  for (let i = 1; i < text.length; i++) {
    assert.deepEqual(show([text.slice(0, i), text.slice(i)], opts), want, `cut at ${i}`);
  }
  assert.deepEqual(show([...text], opts), want, 'one character at a time');
}

test('lines, and the unfinished one', () => {
  everyCut('one\ntwo\n\nthr', ['one', 'two', '', 'thr']);
  everyCut('', ['']);
});

test('a Windows line end is one line end', () => {
  everyCut('one\r\ntwo\r\r\nx', ['one', 'two', 'x']);
});

test('a redrawn line shows its last state', () => {
  everyCut(' 10%\r 20%\r100%\ndone\n', ['100%', 'done', '']);
  // Redrawn by a closing "\r": the state stays until the next one is drawn.
  everyCut('10%\r20%\r', ['20%']);
  everyCut('10%\r20%\r\nx', ['20%', 'x']);
});

test('escape sequences go', () => {
  everyCut('\x1b[1;31mred\x1b[0m ok\n', ['red ok', '']);
  everyCut('\x1b]0;title\x07a\x1b]8;;http://x\x1b\\b\n', ['ab', '']);
  everyCut('\x1b(Ba\x1b[?25lb\x1bMc\n', ['abc', '']);
  everyCut('a\x00\x08b\x7f\tc\n', ['ab\tc', '']);
  assert.equal(plainText('\x1b[32mok\x1b[0m'), 'ok');
});

test('an escape that never ends is not waited for', () => {
  const out = show(['a\x1b]' + 'x'.repeat(5000), 'b\n']);
  assert.match(out[0], /^a\]x+ … \(\+\d+\)$/);
  assert.equal(out.length, 2);
});

test('a long line is cut, saying by how much', () => {
  everyCut('abcdefgh\nab\n', ['abcd … (+4)', 'ab', ''], { maxLine: 4 });
  everyCut('abcdefgh\rxy\n', ['xy', ''], { maxLine: 4 });
  // An endless line keeps only what it shows.
  const log = createLogText({ maxLine: 10 });
  for (let i = 0; i < 1000; i++) log.feed('x'.repeat(100));
  assert.equal(log.feed('').tail, `${'x'.repeat(10)} … (+99990)`);
});

test('emoji and CJK pass through', () => {
  assert.deepEqual(show(['日志', '🎉\n', 'é']), ['日志🎉', 'é']);
});
