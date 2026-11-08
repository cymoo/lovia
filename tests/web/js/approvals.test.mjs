// Approval cards (approvals.js): the question, the scope chips, the outcome.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  approvalHeading,
  approvalOutcome,
  badgeText,
  outcomeText,
  ranAfter,
  scopeLabel,
  scopeList,
  scopePick,
  splitNote,
} from '../../../lovia/web/static/js/approvals.js';
import { setDefaultView } from '../../../lovia/web/static/js/tool-views.js';

test('the question uses the tool label, else names the tool', () => {
  setDefaultView('shell', { label: () => 'Run command' });
  assert.equal(approvalHeading('shell'), 'Run command?');
  assert.equal(approvalHeading('create_schedule'), 'Allow create_schedule?');
});

test('scope chips: the command for shell, a verb for paths, else verbatim', () => {
  assert.equal(scopeLabel('shell:git push'), 'git push');
  assert.equal(scopeLabel('read:~/notes'), 'read ~/notes');
  assert.equal(scopeLabel('write:src'), 'write src');
  assert.equal(scopeLabel('mail:external'), 'mail:external');
  assert.equal(scopeLabel('shell:'), 'shell:');
  assert.equal(scopeLabel('plain'), 'plain');
});

test('scope pick: one click takes all, chips leave some out, order is kept', () => {
  const scopes = ['shell:pwd', 'shell:python', 'shell:git'];
  const pick = scopePick(scopes);
  assert.equal(pick.state(), 'none');
  assert.deepEqual(pick.picked(), []);
  pick.setAll(true);
  assert.equal(pick.state(), 'all');
  pick.toggle('shell:python');
  assert.equal(pick.state(), 'some');
  assert.deepEqual(pick.picked(), ['shell:pwd', 'shell:git']);
  assert.equal(pick.has('shell:python'), false);
  pick.toggle('shell:python');
  assert.equal(pick.state(), 'all');
  pick.setAll(false);
  assert.equal(pick.state(), 'none');
});

test('scope pick: a chip clicked first picks just itself; strangers are ignored', () => {
  const pick = scopePick(['shell:pwd', 'shell:git']);
  pick.toggle('shell:git');
  assert.deepEqual(pick.picked(), ['shell:git']);
  pick.toggle('shell:rm'); // not offered: cannot be picked
  assert.deepEqual(pick.picked(), ['shell:git']);
  pick.toggle('shell:git');
  assert.equal(pick.state(), 'none');
});

test('picked scopes read as a list', () => {
  assert.equal(scopeList(['shell:pwd', 'shell:git']), 'pwd and git');
  assert.equal(scopeList(['read:~/notes']), 'read ~/notes');
});

test("the model's description is set apart from the arguments that act", () => {
  const args = JSON.stringify({ command: 'git status', description: ' Check (read-only) ' });
  assert.deepEqual(splitNote(args), {
    note: 'Check (read-only)',
    rest: JSON.stringify({ command: 'git status' }),
  });
  // Nothing to split: the arguments pass through untouched.
  for (const plain of [
    JSON.stringify({ command: 'ls' }),
    JSON.stringify({ command: 'ls', description: '  ' }),
    JSON.stringify({ description: { nested: true } }),
    '["a"]',
    'not json',
    undefined,
  ]) {
    assert.deepEqual(splitNote(plain), { note: null, rest: plain });
  }
});

test('a frame settles the request by who decided', () => {
  assert.equal(approvalOutcome({ approved: true, cause: 'user' }), 'approve');
  assert.equal(approvalOutcome({ approved: false, cause: 'user' }), 'deny');
  assert.equal(approvalOutcome({ approved: false, cause: 'timeout' }), 'timeout');
  assert.equal(approvalOutcome({ approved: false, cause: 'cancel' }), 'cancel');
  assert.match(outcomeText('timeout'), /denied automatically/);
});

test('the badge names the outcome; only a call that ran gets its own row', () => {
  assert.equal(badgeText('approve'), 'Approved');
  assert.equal(badgeText('auto'), 'Auto-approved');
  assert.equal(badgeText('timeout'), 'Timed out');
  assert.deepEqual(
    ['approve', 'auto', 'deny', 'timeout', 'cancel'].map(ranAfter),
    [true, true, false, false, false],
  );
});
