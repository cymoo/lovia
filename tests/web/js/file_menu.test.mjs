// file-menu.js: when the composer's `@` menu opens, how it matches, what
// picking a file leaves in the message, and when its list is fetched again.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { evaluate, harness, settle } from './composer_harness.mjs';

const { atQuery, matchFiles, insertFile, mentionable } = evaluate(
  ['file-menu.js'],
  ['atQuery', 'matchFiles', 'insertFile', 'mentionable'],
  { store: { on() {} } },
);

test('opens on an `@` that starts the message or follows whitespace', () => {
  assert.equal(atQuery('@', 1), '');
  assert.equal(atQuery('@src/a', 6), 'src/a');
  assert.equal(atQuery('see @app', 8), 'app');
  assert.equal(atQuery('line\n@app', 9), 'app');
  assert.equal(atQuery('tab\t@app', 8), 'app');
  assert.equal(atQuery('@app.py rest', 4), 'app'); // the caret decides
  // Not after a word: an email address, a decorator in pasted code.
  assert.equal(atQuery('me@example.com', 14), null);
  assert.equal(atQuery('x@', 2), null);
  // Past the token: typing the message again.
  assert.equal(atQuery('@app.py ', 8), null);
  assert.equal(atQuery('', 0), null);
  // The quoted form, being typed: spaces belong to the path until the quote.
  assert.equal(atQuery('@"src/my fi', 11), 'src/my fi');
  assert.equal(atQuery('@"src/my file.py" ', 18), null);
  assert.equal(atQuery('@a"b', 4), null);
});

const PATHS = [
  'README.md',
  'docs/guide/app-notes.md',
  'lovia/web/static/js/app.js',
  'src/app.py',
  'src/main.py',
  'src/mapper.py',
  'tests/test_app.py',
];

test('matches file names before folders, then characters in order', () => {
  assert.deepEqual(
    Array.from(matchFiles(PATHS, 'app')),
    [
      'src/app.py', // the name starts with it; shorter first
      'docs/guide/app-notes.md',
      'lovia/web/static/js/app.js',
      'src/mapper.py', // the name contains it ("mApper")
      'tests/test_app.py',
    ],
  );
  assert.deepEqual(Array.from(matchFiles(PATHS, 'SRC/MA')), ['src/main.py', 'src/mapper.py']);
  assert.deepEqual(Array.from(matchFiles(PATHS, 'smp')), ['src/main.py', 'src/mapper.py']);
  assert.deepEqual(Array.from(matchFiles(PATHS, 'zzz')), []);
  // Nothing typed yet: the shortest paths, at most the limit.
  assert.deepEqual(Array.from(matchFiles(PATHS, '', 2)), ['README.md', 'src/app.py']);
});

test('picking puts `@path ` in place of the token, quoting whitespace', () => {
  const at = (text, caret, path) => ({ ...insertFile(text, caret, path) });
  assert.deepEqual(at('@', 1, 'src/app.py'), { text: '@src/app.py ', caret: 12 });
  assert.deepEqual(at('fix @ap', 7, 'src/app.py'), { text: 'fix @src/app.py ', caret: 16 });
  // The token's rest past the caret goes; one following space is reused.
  assert.deepEqual(at('see @ap.p and', 7, 'src/app.py'), {
    text: 'see @src/app.py and',
    caret: 16,
  });
  // Whitespace in the path: quoted.
  assert.deepEqual(at('@my', 3, 'src/my file.py'), {
    text: '@"src/my file.py" ',
    caret: 18,
  });
  // A quoted token being typed is replaced through its closing quote.
  assert.deepEqual(at('@"src/my f" next', 9, 'src/my file.py'), {
    text: '@"src/my file.py" next',
    caret: 18,
  });
  assert.deepEqual(at('a\n@x', 4, 'b.txt'), { text: 'a\n@b.txt ', caret: 9 });
});

test('a path a mention cannot spell is left out', () => {
  assert.ok(mentionable('src/my file.py'));
  assert.ok(mentionable('文档/说明.md'));
  assert.ok(!mentionable('say "hi".txt'));
  assert.ok(!mentionable('two\nlines.txt'));
  assert.ok(!mentionable('tab\there.txt')); // a control character, like the rest
  assert.ok(!mentionable(`bell${String.fromCharCode(7)}.txt`));
  assert.ok(!mentionable(`sep${String.fromCharCode(0x2028)}.txt`));
});

// ---- initFileMenu against a minimal fake DOM ------------------------------

const menu = (opts) => harness('file-menu.js', 'initFileMenu', 'workspacePaths', opts);
const LIST = { paths: ['src/app.py', 'src/my file.py', 'bad"name.txt'], truncated: false };

test('lists the files, picks with the keyboard, and the composer does not send', async () => {
  const { input, type, fetches, rows, note } = menu();
  type('see @');
  assert.deepEqual({ ...fetches[0].where }, { session: 's1' });
  fetches[0].resolve(LIST);
  await settle();
  assert.deepEqual(rows(), ['app.py', 'my file.py']); // the quote-bearing path is out
  assert.equal(note(), null);
  type('see @my');
  assert.deepEqual(rows(), ['my file.py']);
  let stopped = false;
  input.fire('keydown', { key: 'Tab', stopImmediatePropagation: () => (stopped = true) });
  assert.equal(input.value, 'see @"src/my file.py" ');
  assert.ok(stopped);
});

test('a cut list says so under the rows', async () => {
  const { type, fetches, note } = menu();
  type('@');
  fetches[0].resolve({ paths: ['a.txt', 'b.txt'], truncated: true });
  await settle();
  assert.equal(note(), 'composer.filesTruncated {"n":2}');
});

test('Esc keeps the menu shut for that token only: a send starts afresh', async () => {
  const { input, type, fetches, isOpen } = menu();
  type('@a');
  fetches[0].resolve(LIST);
  await settle();
  assert.ok(isOpen());
  input.fire('keydown', { key: 'Escape' });
  type('@ap');
  await settle();
  assert.ok(!isOpen());
  input.form.fire('submit'); // clears the box itself — no input event
  type('@');
  await settle();
  assert.ok(isOpen()); // from the chat's kept list: no second fetch
  assert.equal(fetches.length, 1);
});

test('an answer that arrives after the menu closed does not open it', async () => {
  const { input, type, fetches, isOpen } = menu();
  type('@');
  input.fire('blur');
  fetches[0].resolve(LIST);
  await settle();
  assert.ok(!isOpen());
  // The answer was still the chat's list: the next `@` uses it.
  input.fire('click');
  await settle();
  assert.ok(isOpen());
  assert.equal(fetches.length, 1);
});

test('a run ending drops the list: the next `@` sees the files it made', async () => {
  const { input, type, fetches, store, rows } = menu();
  type('@');
  fetches[0].resolve(LIST);
  await settle();
  input.fire('blur');
  store.emit('run-end', { sessionId: 's1' });
  type('@');
  assert.equal(fetches.length, 2);
  fetches[1].resolve({ paths: ['new.py', ...LIST.paths], truncated: false });
  await settle();
  assert.deepEqual(rows(), ['new.py', 'app.py', 'my file.py']);
});

test('a list fetched across a run ending is used once, not kept', async () => {
  const { input, type, fetches, store, isOpen } = menu();
  type('@');
  store.emit('run-end', { sessionId: 's1' });
  fetches[0].resolve(LIST);
  await settle();
  assert.ok(isOpen());
  input.fire('blur');
  type('@');
  assert.equal(fetches.length, 2);
});

test('each chat keeps its own list', async () => {
  const { input, type, fetches, store } = menu();
  type('@');
  fetches[0].resolve(LIST);
  await settle();
  input.fire('blur');
  store.sessionId = 's2';
  type('@');
  assert.deepEqual({ ...fetches[1].where }, { session: 's2' });
});

test('no workspace, no fetch and no menu', async () => {
  const { type, fetches, isOpen } = menu({ store: { agents: [{ name: 'a' }] } });
  type('@');
  await settle();
  assert.equal(fetches.length, 0);
  assert.ok(!isOpen());
});

test('a failed fetch shows nothing and is retried on the next keystroke', async () => {
  const { type, fetches, isOpen } = menu();
  type('@');
  fetches[0].reject(new Error('502'));
  await settle();
  assert.ok(!isOpen());
  type('@s');
  assert.equal(fetches.length, 2);
});
