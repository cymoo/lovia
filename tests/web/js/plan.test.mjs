// The plan (plan.js): parsing an update, what it changed, the row line, the
// current step, and the per-chat history rows diff against.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  currentStep,
  isPlanResult,
  notePlan,
  parsePlan,
  planBefore,
  planDiff,
  planSummary,
  planText,
  resetPlans,
  usePlanView,
} from '../../../lovia/web/static/js/plan.js';
import { toolLabel, toolViewChain } from '../../../lovia/web/static/js/tool-views.js';

/** @param {string} s A compact plan: one char per item (x done, ~ now, . pending). */
function items(s, names = 'abcdefgh') {
  const status = { x: 'completed', '~': 'in_progress', '.': 'pending' };
  return [...s].map((c, i) => ({ content: names[i], status: status[c], active_form: null }));
}

test('parses a call or its arguments object, and nothing else', () => {
  const args = { todos: [{ content: 'a', status: 'in_progress', active_form: 'Doing a' }, { content: 'b' }] };
  const want = [
    { content: 'a', status: 'in_progress', active_form: 'Doing a' },
    { content: 'b', status: 'pending', active_form: null },
  ];
  assert.deepEqual(parsePlan(JSON.stringify(args)), want);
  assert.deepEqual(parsePlan(args), want);
  assert.equal(parsePlan('{"path": "x"}'), null);
  assert.equal(parsePlan('not json'), null);
  assert.equal(parsePlan(undefined), null);
  // An unknown status reads as pending rather than breaking the list.
  assert.equal(parsePlan({ todos: [{ content: 'a', status: 'blocked' }] })[0].status, 'pending');
});

test('arguments read as the plugin stored them: one item in progress', () => {
  const args = { todos: [{ content: 'a', status: 'in_progress' }, { content: 'b', status: 'in_progress' }] };
  assert.deepEqual(parsePlan(args).map((i) => i.status), ['in_progress', 'pending']);
  // So a later update that starts b reports it, as the live list showed it.
  const next = parsePlan({ todos: [{ content: 'a', status: 'completed' }, { content: 'b', status: 'in_progress' }] });
  assert.equal(planSummary(parsePlan(args), next), '1/2 · Done “a” · Now “b”');
});

test('items sharing a text match one for one', () => {
  const prev = [
    { content: 'test', status: 'pending', active_form: null },
    { content: 'test', status: 'completed', active_form: null },
  ];
  const next = [{ content: 'test', status: 'completed', active_form: null }];
  // The first prev "test" pairs with it (now done); the second is gone.
  assert.equal(planSummary(prev, next), '1/1 · Done “test” · 1 removed');
  assert.equal(planSummary(prev, prev), '1/2 · Revised the plan');
});

test('a row line says progress, then what changed', () => {
  assert.equal(planSummary(null, items('~..')), '0/3 · Made a plan');
  assert.equal(planSummary(items('~..'), []), 'Cleared the plan');
  assert.equal(planSummary(items('~..'), items('x~.')), '1/3 · Done “a” · Now “b”');
  assert.equal(planSummary(items('~~.'), items('xx~')), '2/3 · 2 steps done · Now “c”');
  // Matched by text: a new item is added, a missing one removed.
  assert.equal(planSummary(items('x~'), items('x~.', 'abz')), '1/3 · 1 added');
  assert.equal(planSummary(items('x~.'), items('x~')), '1/2 · 1 removed');
  assert.equal(planSummary(items('x~'), items('x~')), '1/2 · Revised the plan');
  // The step now in progress reads in its present-tense form.
  const next = [{ content: 'Fix it', status: 'in_progress', active_form: 'Fixing it' }];
  assert.equal(planSummary([{ content: 'Fix it', status: 'pending', active_form: null }], next), '0/1 · Now “Fixing it”');
});

test('a diff marks new items and moved statuses, none for a first plan', () => {
  assert.deepEqual([...planDiff(items('~..'), items('x~.')).changed], [0, 1]);
  assert.deepEqual([...planDiff(items('x~'), items('x~.', 'abz')).changed], [2]);
  assert.deepEqual([...planDiff(null, items('~..')).changed], []);
});

test('the current step: in progress, else the next pending, else none', () => {
  assert.deepEqual(currentStep(items('x~.')), { item: items('x~.')[1], next: false });
  assert.deepEqual(currentStep(items('x..')), { item: items('x..')[1], next: true });
  assert.equal(currentStep(items('xx')), null);
});

test('the copy text and its recognition mirror the plugin', () => {
  const text = planText(items('x~.'));
  assert.equal(text, 'Updated todo list:\n[x] a\n[~] b\n[ ] c');
  assert.ok(isPlanResult(text));
  assert.ok(isPlanResult(planText([])));
  assert.ok(!isPlanResult('Read 3 lines'));
  assert.ok(!isPlanResult(undefined));
});

test('each update diffs against the one before it; failed ones are skipped', () => {
  resetPlans();
  notePlan('c1', items('~.'));
  // c2 failed: never noted. A running update reads against the latest.
  assert.deepEqual(planBefore('c2'), items('~.'));
  notePlan('c3', items('x~'));
  assert.deepEqual(planBefore('c3'), items('~.'));
  assert.equal(planBefore('c1'), null);
  // Noted again (a redraw), it keeps what it replaced.
  notePlan('c3', items('x~'));
  assert.deepEqual(planBefore('c3'), items('~.'));
  resetPlans();
  assert.equal(planBefore('c3'), null);
});

test('a plan row is a noted update, whatever its tool is called', () => {
  resetPlans();
  usePlanView('set_plan');
  assert.equal(toolLabel('set_plan'), 'Plan');
  const [summary] = toolViewChain('set_plan', 'summary');
  const tool = (id, args) => ({ id, name: 'set_plan', args, status: 'done', isError: false });
  const args = { todos: [{ content: 'a' }] };
  // Not noted (still running, failed, or another tool of that name): its own line.
  assert.equal(summary(tool('c1', args)), null);
  notePlan('c1', parsePlan(args));
  assert.equal(summary(tool('c1', args)), '0/1 · Made a plan');
});
