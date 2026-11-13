// skill-menu.js: when the composer's `/` menu opens, what it offers, and
// what picking a skill leaves in the message.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../../../lovia/web/static/js/skill-menu.js', import.meta.url), 'utf8')
  .replace(/^import .*;$/gm, '')
  .replace(/^export /gm, '');

const context = {};
runInNewContext(
  `${source}\nglobalThis.slashQuery = slashQuery;` +
    `\nglobalThis.matchSkills = matchSkills;\nglobalThis.insertSkill = insertSkill;`,
  context,
);
const { slashQuery, matchSkills, insertSkill } = context;

test('opens only on a `/name` that starts the message', () => {
  assert.equal(slashQuery('/', 1), '');
  assert.equal(slashQuery('/rev', 4), 'rev');
  assert.equal(slashQuery('/rev', 2), 'r'); // the caret decides
  assert.equal(slashQuery('/rev and more', 4), 'rev');
  // Anywhere else, the model's rule ignores it — so does the menu.
  assert.equal(slashQuery(' /rev', 5), null);
  assert.equal(slashQuery('hi\n/rev', 7), null);
  assert.equal(slashQuery('hi /rev', 7), null);
  // Past the name: typing the message, not the name.
  assert.equal(slashQuery('/review ', 8), null);
  // A path is not a skill name.
  assert.equal(slashQuery('/src/foo.py', 11), null);
  assert.equal(slashQuery('/src/', 5), null);
  assert.equal(slashQuery('', 0), null);
});

const skills = [
  { name: 'review', description: 'r' },
  { name: 'pre-review', description: 'p' },
  { name: 'Init', description: 'i' },
  { name: 'deploy', description: 'd' },
];

test('filters by name: prefix matches first, then the rest, in catalog order', () => {
  const names = (q) => Array.from(matchSkills(skills, q), (s) => s.name);
  assert.deepEqual(names(''), ['review', 'pre-review', 'Init', 'deploy']);
  assert.deepEqual(names('rev'), ['review', 'pre-review']);
  assert.deepEqual(names('pre'), ['pre-review']);
  assert.deepEqual(names('init'), ['Init']); // case-insensitive
  assert.deepEqual(names('x'), []);
});

test('picking puts `/name ` at the start, replacing the name being typed', () => {
  assert.deepEqual({ ...insertSkill('/', 'review') }, { text: '/review ', caret: 8 });
  assert.deepEqual({ ...insertSkill('/re', 'review') }, { text: '/review ', caret: 8 });
  // The rest of the message stays; one separating space is reused, not doubled.
  assert.deepEqual({ ...insertSkill('/re the diff', 'review') }, { text: '/review the diff', caret: 8 });
  assert.deepEqual({ ...insertSkill('/rexx', 'review') }, { text: '/review ', caret: 8 });
  assert.deepEqual({ ...insertSkill('/\nline two', 'init') }, { text: '/init \nline two', caret: 6 });
});

// ---- initSkillMenu against a minimal fake DOM -----------------------------

/** A stand-in element: attributes, listeners (fired by hand), children. */
function fakeElement(onRemove = () => {}) {
  const listeners = {};
  const attrs = {};
  return {
    style: {},
    children: [],
    setAttribute(k, v) {
      attrs[k] = String(v);
    },
    getAttribute: (k) => attrs[k] ?? null,
    removeAttribute(k) {
      delete attrs[k];
    },
    addEventListener(type, fn) {
      (listeners[type] ||= []).push(fn);
    },
    fire(type, props = {}) {
      for (const fn of listeners[type] || []) {
        fn({ type, preventDefault() {}, stopImmediatePropagation() {}, ...props });
      }
    },
    append(...c) {
      this.children.push(...c);
    },
    replaceChildren(...c) {
      this.children = c;
    },
    remove() {
      onRemove(this);
    },
    showPopover() {},
    scrollIntoView() {},
    getBoundingClientRect: () => ({ left: 0, top: 500, bottom: 540, width: 300, height: 100 }),
  };
}

/** The menu wired to a fake composer; `fetches` are the pending
 * `api.listSkills` calls, each resolved by the test. */
function harness() {
  const open = new Set();
  const fetches = [];
  const input = Object.assign(fakeElement(), {
    value: '',
    selectionStart: 0,
    selectionEnd: 0,
    form: fakeElement(),
    closest: () => null,
    focus() {},
    setSelectionRange(a, b) {
      this.selectionStart = a;
      this.selectionEnd = b;
    },
    dispatchEvent(ev) {
      this.fire(ev.type);
    },
  });
  const context = {
    api: {
      listSkills: () =>
        new Promise((resolve) => {
          fetches.push(resolve);
        }),
    },
    store: { sessionId: 's1', agent: 'a', on() {} },
    t: (k) => k,
    draftWorkspaceRoot: () => null,
    document: {
      createElement: () => fakeElement((m) => open.delete(m)),
      body: { append: (m) => open.add(m) },
    },
    window: { addEventListener() {} },
    innerWidth: 1000,
    Event: class {
      constructor(type) {
        this.type = type;
      }
    },
  };
  runInNewContext(`${source}\nglobalThis.initSkillMenu = initSkillMenu;`, context);
  context.initSkillMenu(input);
  const type = (text) => {
    input.value = text;
    input.selectionStart = input.selectionEnd = text.length;
    input.fire('input');
  };
  return { input, type, fetches, isOpen: () => open.size > 0 };
}

const settle = () => new Promise((r) => setImmediate(r));
const SKILLS = [{ name: 'review', description: 'r' }];

test('Esc keeps the menu shut for that message only: a send starts afresh', async () => {
  const { input, type, fetches, isOpen } = harness();
  type('/rev');
  fetches[0](SKILLS);
  await settle();
  assert.ok(isOpen());
  input.fire('keydown', { key: 'Escape' });
  assert.ok(!isOpen());
  type('/revi'); // dismissed: typing on in the same name stays shut
  for (const resolve of fetches) resolve(SKILLS);
  await settle();
  assert.ok(!isOpen());
  assert.equal(fetches.length, 1);
  // The composer sends and clears the box itself — no input event.
  input.form.fire('submit');
  input.value = '';
  input.selectionStart = input.selectionEnd = 0;
  type('/');
  fetches[fetches.length - 1](SKILLS);
  await settle();
  assert.ok(isOpen());
});

test('an answer that arrives after the menu closed is dropped', async () => {
  const { input, type, fetches, isOpen } = harness();
  type('/');
  input.fire('blur');
  fetches[0](SKILLS);
  await settle();
  assert.ok(!isOpen());
  assert.equal(fetches.length, 1); // and it does not go fetching again
});

test('reopened while an old fetch is out: the new opening fetches for itself', async () => {
  const { input, type, fetches, isOpen } = harness();
  type('/');
  input.fire('blur');
  input.fire('click'); // back in the box, still on the `/`
  assert.equal(fetches.length, 2);
  fetches[0]([]); // the stale answer: ignored
  await settle();
  assert.ok(!isOpen());
  fetches[1](SKILLS);
  await settle();
  assert.ok(isOpen());
  type('/x');
  type('/');
  await settle();
  assert.ok(isOpen()); // the list fetched for this opening is kept
  assert.equal(fetches.length, 2);
});
