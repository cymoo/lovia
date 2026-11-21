// The plan (plan.js): parsing an update, the row line (where the plan stands,
// no diff), the current step, how a replay knows an update, and plan rows.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  currentStep,
  isPlanOutput,
  markPlanRow,
  parsePlan,
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
});

test('a row line says where the plan stands, from its own list alone', () => {
  assert.equal(planSummary(items('~..')), 'Plan 0/3 · Now “a”');
  assert.equal(planSummary(items('x..')), 'Plan 1/3 · Next: b');
  assert.equal(planSummary(items('xxx')), 'Plan 3/3 · All done');
  assert.equal(planSummary([]), 'Cleared the plan');
  // The step in progress reads in its present-tense form.
  const doing = [{ content: 'Fix it', status: 'in_progress', active_form: 'Fixing it' }];
  assert.equal(planSummary(doing), 'Plan 0/1 · Now “Fixing it”');
});

test('the current step: in progress, else the next pending, else none', () => {
  assert.deepEqual(currentStep(items('x~.')), { item: items('x~.')[1], next: false });
  assert.deepEqual(currentStep(items('x..')), { item: items('x..')[1], next: true });
  assert.equal(currentStep(items('xx')), null);
});

// Pinned on the Python side too (tests/plugins/test_todos.py): a replayed
// call is a plan update only when its result is exactly this text.
test('the text mirrors the plugin’s own result', () => {
  assert.equal(planText(items('x~.')), 'Updated todo list:\n[x] a\n[~] b\n[ ] c');
  const doing = [{ content: 'b', status: 'in_progress', active_form: 'Doing b' }];
  assert.equal(planText(doing), 'Updated todo list:\n[~] Doing b');
  assert.equal(planText([]), 'Todo list cleared.');
});

// Pinned on the Python side too (tests/plugins/test_todos.py): the agent's
// max_tool_output_chars cuts a long result this way.
const CUT =
  'Updated todo list:\n[x] Read the \n\n[... tool output truncated: kept 40 of 103 chars ...]\n\n Ship it';

test('a replay knows a plan update by its result, whole or truncated', () => {
  const plan = parsePlan({
    todos: [
      { content: 'Read the failing test', status: 'completed' },
      { content: 'Fix the key', status: 'in_progress', active_form: 'Fixing the key 🔑' },
      { content: 'Add a regression test' },
      { content: 'Ship it' },
    ],
  });
  assert.ok(isPlanOutput(planText(plan), plan));
  assert.ok(isPlanOutput(CUT, plan)); // 103 code points, though 104 UTF-16 units
  assert.ok(!isPlanOutput(CUT.replace('of 103', 'of 99'), plan));
  assert.ok(!isPlanOutput(CUT.replace('Ship it', 'Ship that'), plan));
  assert.ok(!isPlanOutput('Updated todo list:\n[ ] something else', plan));
  // A clear's confirmation, cut by a tiny limit (truncate_tool_output(…, 10)).
  const cutClear = 'Todo lis\n\n[... tool output truncated: kept 10 of 18 chars ...]\n\nd.';
  assert.ok(isPlanOutput(cutClear, []));
  assert.ok(!isPlanOutput(cutClear, plan));
});

test('a plan view layers over a built-in tool of the same name', async () => {
  await import('../../../lovia/web/static/js/builtin-views.js');
  resetPlans();
  const before = toolViewChain('start_session', 'render')[0];
  usePlanView('start_session');
  // Its other calls keep their own line, label and card.
  assert.equal(toolViewChain('start_session', 'render')[0], before);
  assert.equal(toolLabel('start_session'), 'Start a chat');
  const [summary] = toolViewChain('start_session', 'summary');
  const call = { id: 's1', key: 1, name: 'start_session', args: { title: 'x' }, status: 'done', isError: false };
  assert.equal(summary(call), 'Start a chat “x”');
});

test('a plan row is a marked card, whatever its tool is called', () => {
  resetPlans();
  usePlanView('set_plan');
  // The name says nothing about one call: the digest label is the card's own.
  assert.equal(toolLabel('set_plan'), 'set_plan');
  const [summary] = toolViewChain('set_plan', 'summary');
  const tool = (key, args) => ({ id: 'call_0', key, name: 'set_plan', args, status: 'done', isError: false });
  const args = { todos: [{ content: 'a' }] };
  // Not marked (still running, failed, or another call of that tool): its own line.
  assert.equal(summary(tool(11, args)), null);
  markPlanRow(11);
  assert.equal(summary(tool(11, args)), 'Plan 0/1 · Next: a');
  assert.equal(summary(tool(12, args)), null); // same id, another card
  resetPlans();
  assert.equal(summary(tool(11, args)), null);
});
