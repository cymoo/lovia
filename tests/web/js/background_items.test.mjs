// Tasks and commands as one list (background-items.js).
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  buildItems,
  buttonState,
  createFailureWatch,
  taskLabel,
  taskState,
} from '../../../lovia/web/static/js/background-items.js';

const cmd = (id, over = {}) => ({
  process_id: id,
  command: `run ${id}`,
  status: 'running',
  exit_code: null,
  started_at: 100,
  ended_at: null,
  ended_by: null,
  last_line: '',
  ...over,
});
const done = (id, code, over = {}) =>
  cmd(id, { status: 'exited', exit_code: code, ended_by: 'exit', ended_at: 110, ...over });
const task = (id, over = {}) => ({ id, title: `[t1] ${id}`, created_at: 100, ...over });
const runs = (...rows) => new Map(rows.map((r) => [r.session_id, r]));
const keys = (list) => list.map((i) => i.key);

test('a task waits on the user only when its run says so', () => {
  const t = task('a');
  assert.equal(taskState(t, { status: 'running' }), 'running');
  // Not running and not waiting either: winding down, starting up.
  assert.equal(taskState(t, { status: 'cancelling' }), 'running');
  assert.equal(taskState(t, { status: 'blocked_on_approval', pending_approval: {} }), 'approval');
  assert.equal(taskState(t, { status: 'waiting', waits_on: { kind: 'question' } }), 'approval');
  // Stopped while a tool of its still runs: not over yet, whatever lists it.
  const stopping = task('a', { last_run_status: 'running' });
  assert.equal(taskState(stopping), 'stopping');
  assert.equal(buildItems([], [stopping], undefined).live.length, 1);
  assert.equal(taskState(task('a', { last_run_status: 'failed' })), 'failed');
  assert.equal(taskState(task('a', { last_run_status: 'cancelled' })), 'stopped');
  assert.equal(taskState(task('a', { last_run_status: 'interrupted' })), 'interrupted');
  assert.equal(taskState(task('a')), 'completed');
});

test('a running task is in a step, an ended one took a time', () => {
  const step = { name: 'read_file', args: { path: 'a.md' } };
  const [live] = buildItems([], [task('a')], runs({ session_id: 'a', started_at: 150, step })).live;
  assert.deepEqual([live.step, live.startedAt, live.endedAt], [step, 150, null]);
  const [ended] = buildItems([], [task('a', { last_run_seconds: 90 })], undefined).ended;
  assert.deepEqual([ended.step, ended.endedAt - ended.startedAt], [null, 90]);
});

test('a task is named by its title', () => {
  assert.deepEqual(taskLabel(task('dig in')), { tag: 't1', title: 'dig in' });
  assert.deepEqual(taskLabel({ id: 'x', title: 'plain' }), { tag: '', title: 'plain' });
  assert.deepEqual(taskLabel({ id: 'x', title: null }), { tag: '', title: 'x' });
  assert.deepEqual(taskLabel({ id: 'x', title: '[t2] ' }), { tag: 't2', title: 'x' });
});

test('how a command ended', () => {
  const state = (p) => {
    const { live, ended } = buildItems([p], [], undefined);
    return [...live, ...ended][0];
  };
  assert.equal(state(cmd('a')).state, 'running');
  assert.deepEqual([state(done('a', 0)).state, state(done('a', 0)).failed], ['completed', false]);
  assert.deepEqual([state(done('a', 2)).state, state(done('a', 2)).failed], ['failed', true]);
  const killed = { status: 'killed', exit_code: -9 };
  for (const by of ['user', 'model', 'shutdown', 'closed']) {
    const item = state(cmd('a', { ...killed, ended_by: by }));
    assert.deepEqual([item.state, item.failed, item.endedBy], ['stopped', false, by]);
  }
  assert.equal(state(cmd('a', { ...killed, ended_by: 'lost' })).state, 'lost');
  // One line, whatever the command; its last line only while it runs.
  const item = state(cmd('a', { command: 'a \\\n  b\n', last_line: 'x' }));
  assert.deepEqual([item.title, item.line], ['a \\ b', 'x']);
  assert.equal(state(done('a', 0, { last_line: 'x' })).line, '');
});

test('what waits on the user leads, then newest first', () => {
  const { live, ended } = buildItems(
    [cmd('a', { started_at: 300 }), done('b', 0, { started_at: 50 }), done('c', 1, { started_at: 70 })],
    [task('w', { created_at: 10 }), task('r', { created_at: 20 }), task('z', { created_at: 60 })],
    runs(
      { session_id: 'w', status: 'waiting', started_at: 10, waits_on: { kind: 'approval' } },
      { session_id: 'r', status: 'running', started_at: 200 },
    ),
  );
  assert.deepEqual(keys(live), ['task:w', 'cmd:a', 'task:r']);
  assert.deepEqual(live.map((i) => i.waitsFor), ['approval', null, null]);
  assert.deepEqual(keys(ended), ['cmd:c', 'task:z', 'cmd:b']);
});

test('the button shows the first that holds', () => {
  const state = (procs, tasks, live, unseen = []) =>
    buttonState(buildItems(procs, tasks, live), new Set(unseen));
  const waiting = runs({ session_id: 'a', waits_on: { kind: 'approval' } });
  const running = runs({ session_id: 'a', status: 'running' });
  const failed = done('x', 1);
  assert.equal(state([failed], [task('a')], waiting, ['cmd:x']).tone, 'approval');
  assert.equal(state([failed], [task('a')], running, ['cmd:x']).tone, 'failed');
  assert.equal(state([failed], [task('a')], running).tone, 'running');
  assert.equal(state([cmd('y'), failed], [], undefined).tone, 'steady');
  assert.deepEqual(state([failed], [task('a')], undefined), {
    tone: 'idle',
    running: 0,
    total: 2,
  });
  assert.deepEqual(state([cmd('y'), failed], [task('a')], running).running, 2);
});

test('only failures seen to happen are news', () => {
  let now = 1000;
  const watch = createFailureWatch(() => now);
  const list = (...procs) => buildItems(procs, [], undefined);
  const late = { started_at: 1010 };
  // Over when the chat was opened — though its list lands only later.
  watch.take('s', list());
  watch.take('s', list(done('old', 1), cmd('a')));
  assert.deepEqual([...watch.unseen], []);
  watch.take('s', list(done('old', 1), done('a', 1), cmd('b')));
  assert.deepEqual([...watch.unseen], ['cmd:a']);
  watch.seen();
  // Told once; one that came and failed between two looks counts too.
  watch.take('s', list(done('old', 1), done('a', 1), done('b', 0), done('c', 1, late)));
  assert.deepEqual([...watch.unseen], ['cmd:c']);
  // Another chat starts from what it has.
  now = 2000;
  watch.take('t', list(done('x', 1, late)));
  assert.deepEqual([...watch.unseen], []);
  // A row that went takes its mark with it.
  watch.take('t', list(done('x', 1, late), done('y', 1, { started_at: 2001 })));
  assert.deepEqual([...watch.unseen], ['cmd:y']);
  watch.take('t', list(done('x', 1, late)));
  assert.deepEqual([...watch.unseen], []);
});

test('a task that runs again may fail again', () => {
  const watch = createFailureWatch(() => 1000);
  const list = (run, status) =>
    buildItems([], [task('a', { last_run_status: status })], run ? runs({ session_id: 'a' }) : undefined);
  watch.take('s', list(true));
  watch.take('s', list(false, 'failed'));
  assert.deepEqual([...watch.unseen], ['task:a']);
  watch.seen();
  watch.take('s', list(true, 'failed'));
  watch.take('s', list(false, 'failed'));
  assert.deepEqual([...watch.unseen], ['task:a']);
});
