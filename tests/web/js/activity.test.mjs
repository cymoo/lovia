// Activity groups (activity.js): the one-line digest of a folded step run.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { summarize } from '../../../lovia/web/static/js/activity.js';

test('thought first, then each tool once, in order, counted when repeated', () => {
  const steps = [
    { reasoning: true },
    { tool: 'read_file' },
    { reasoning: true },
    { tool: 'shell' },
    { tool: 'read_file' },
  ];
  assert.deepEqual(summarize(steps), ['Thought', 'read_file ×2', 'shell']);
});

test('no reasoning, no "Thought"; steps with neither are ignored', () => {
  assert.deepEqual(summarize([{ tool: 'shell' }, {}]), ['shell']);
});

test('past the cap, the rest collapse into +N', () => {
  const steps = ['a', 'b', 'c', 'd', 'e', 'f'].map((tool) => ({ tool }));
  assert.deepEqual(summarize(steps, 4), ['a', 'b', 'c', 'd', '+2']);
});
