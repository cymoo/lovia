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
