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
  pendingTaskApprovals,
  ranAfter,
  scopeLabel,
  scopeList,
  scopePick,
  shellArgs,
  shellMeta,
  foldCommand,
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
  assert.equal(scopeLabel('mcp:github__create_issue'), 'github__create_issue');
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
  assert.equal(approvalOutcome({ approved: false, cause: 'cancel' }), 'cancel');
  // The chat's mode, switched while it waited, decides like a person.
  assert.equal(approvalOutcome({ approved: true, cause: 'policy' }), 'approve');
  assert.equal(approvalOutcome({ approved: false, cause: 'policy' }), 'deny');
  assert.match(outcomeText('cancel'), /stopped/);
});

test('the badge names the outcome; only a call that ran gets its own row', () => {
  assert.equal(badgeText('approve'), 'Approved');
  assert.equal(badgeText('auto'), 'Auto-approved');
  assert.deepEqual(
    ['approve', 'auto', 'deny', 'cancel'].map(ranAfter),
    [true, true, false, false],
  );
});

test("a chat offers its own tasks' pending approvals, less those it answered", () => {
  const call = { id: 'c1', name: 'shell' };
  const tasks = [
    { id: 't1', parent_id: 'p' },
    { id: 't2', parent_id: 'p' }, // running, nothing pending
    { id: 't3', parent_id: 'p' },
  ];
  const runs = new Map([
    ['t1', { pending_approval: call }],
    ['t2', { pending_approval: null }],
    ['t3', { pending_approval: { id: 'c9', name: 'shell' } }],
  ]);
  const keys = (want) => [...want.keys()];
  assert.deepEqual(keys(pendingTaskApprovals(tasks, runs, 'p', new Map())), ['t1:c1', 't3:c9']);
  assert.equal(pendingTaskApprovals(tasks, runs, 'p', new Map()).get('t1:c1').call, call);
  // Answered here: kept out while a snapshot from before the answer shows it.
  const decided = new Map([['t1', JSON.stringify(call)]]);
  assert.deepEqual(keys(pendingTaskApprovals(tasks, runs, 'p', decided)), ['t3:c9']);
  assert.equal(decided.size, 1);
  // Another chat is open, its own task list not loaded yet: offer nothing.
  assert.deepEqual(keys(pendingTaskApprovals(tasks, runs, 'q', new Map())), []);
  assert.deepEqual(keys(pendingTaskApprovals(tasks, undefined, 'p', new Map())), []);
});

test('an answer here hides only the request it answered', () => {
  const tasks = [{ id: 't1', parent_id: 'p' }];
  const first = { id: 'c1', name: 'shell', arguments: '{"command":"ls"}', expires_at: 10 };
  const decided = new Map([['t1', JSON.stringify(first)]]);
  // A new request under a reused call id — with no event seen in between
  // (polling, a reconnect) — is offered, and the old answer forgotten.
  const next = { ...first, arguments: '{"command":"rm x"}', expires_at: 20 };
  const want = pendingTaskApprovals(tasks, new Map([['t1', { pending_approval: next }]]), 'p', decided);
  assert.equal(want.get('t1:c1').call, next);
  assert.equal(decided.size, 0);
  // A snapshot with nothing pending forgets it too.
  decided.set('t1', JSON.stringify(first));
  pendingTaskApprovals(tasks, new Map([['t1', { pending_approval: null }]]), 'p', decided);
  assert.equal(decided.size, 0);
});

test('shell card: the command alone, with only the non-default options', () => {
  const args = (o) => JSON.stringify(o);
  assert.deepEqual(shellArgs('shell', args({ command: 'ls' })), {
    command: 'ls', cwd: null, timeout: null, background: false,
  });
  // Defaults say nothing; a set option is kept.
  assert.deepEqual(
    shellArgs('shell', args({ command: 'ls', cwd: '.', timeout: null, background: false, description: ' ' })),
    { command: 'ls', cwd: null, timeout: null, background: false },
  );
  assert.deepEqual(shellArgs('shell', args({ command: 'make', cwd: 'src', timeout: 600, background: true })), {
    command: 'make', cwd: 'src', timeout: 600, background: true,
  });
  // Anything else falls back to the generic rows, which show it all.
  for (const other of [
    args({ command: 'ls', env: { A: '1' } }),
    args({ command: ['ls'] }),
    args({ command: 'ls', cwd: 3 }),
    args({ command: 'ls', description: 'not split off' }),
    'not json',
    undefined,
  ]) {
    assert.equal(shellArgs('shell', other), null);
  }
  assert.equal(shellArgs('read_file', args({ command: 'ls' })), null);
  assert.equal(shellMeta({ cwd: null, timeout: null, background: false }), '');
  assert.equal(shellMeta({ cwd: 'src', timeout: 600, background: false }), 'in src · timeout 600s');
  // The background path ignores timeout, so the card does not promise one.
  assert.equal(shellMeta({ cwd: 'src', timeout: 600, background: true }), 'in src · in the background');
});

test('a long command folds, and says how much it hides', () => {
  const lines = (n) => Array.from({ length: n }, (_, i) => `line ${i + 1}`).join('\n');
  assert.deepEqual(foldCommand(lines(12)), { shown: lines(12), hidden: 0 });
  assert.deepEqual(foldCommand(lines(20)), { shown: lines(20), hidden: 0 });
  // A trailing newline is not a line of its own.
  assert.deepEqual(foldCommand(`${lines(20)}\n`), { shown: `${lines(20)}\n`, hidden: 0 });
  assert.deepEqual(foldCommand(lines(30)), { shown: lines(15), hidden: 15 });
  assert.deepEqual(foldCommand(lines(21)), { shown: lines(15), hidden: 6 });
});
