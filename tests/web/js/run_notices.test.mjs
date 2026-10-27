import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRunNotices } from '../../../lovia/web/static/js/run-notices.js';

const run = (id, status = 'completed', session_id = 'chat') => ({ id, status, session_id });
function fixture(t) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const shown = [];
  const notices = createRunNotices({
    notify: (message, opts) => shown.push({ ...message, ...opts }),
    t: (key, params) => ({ messageKey: key, params }),
    titleOf: (sid) => `Title ${sid}`,
  });
  return { ...notices, shown, tick: (ms) => t.mock.timers.tick(ms) };
}

test('refresh results become one summary with accurate completed and failed counts', (t) => {
  const f = fixture(t);
  f.add([run('a'), run('b'), run('c', 'failed'), run('d', 'cancelled'), run('e', 'interrupted')], { missed: true });
  assert.equal(f.shown.length, 1);
  assert.equal(f.shown[0].messageKey, 'toast.missedRunsMixed');
  assert.deepEqual(f.shown[0].params, { n: 3, completed: 2, failed: 1 });
  assert.equal(f.shown[0].type, 'error');
});

test('single results retain the chat title; homogeneous batches use the correct summary', (t) => {
  const f = fixture(t);
  f.add([run('a', 'failed', 'A')], { missed: true });
  assert.equal(f.shown[0].messageKey, 'toast.missedRunFailed');
  assert.deepEqual(f.shown[0].params, { title: 'Title A' });
  f.add([run('b'), run('c')], { missed: true });
  assert.equal(f.shown[1].messageKey, 'toast.missedRunsFinished');
  f.add([run('d', 'failed'), run('e', 'failed')], { missed: true });
  assert.equal(f.shown[2].messageKey, 'toast.missedRunsFailed');
});

test('live arrivals group in a fixed window rather than continually deferring feedback', (t) => {
  const f = fixture(t);
  f.add([run('a')]);
  f.tick(900);
  f.add([run('b', 'failed')]);
  assert.equal(f.shown.length, 0);
  f.tick(100);
  assert.equal(f.shown[0].messageKey, 'toast.runsMixed');
  assert.deepEqual(f.shown[0].params, { n: 2, completed: 1, failed: 1 });
  f.add([run('c')]);
  f.tick(1000);
  assert.equal(f.shown[1].messageKey, 'toast.runFinished');
  assert.notEqual(f.shown[0].key, f.shown[1].key); // success cannot overwrite failure
});

test('history and live results deduplicate in either order, also within a response', (t) => {
  const f = fixture(t);
  f.add([run('a'), run('a')], { missed: true });
  assert.equal(f.add([run('a')]), false);
  f.add([run('b')]);
  assert.equal(f.add([run('b')], { missed: true }), false);
  f.tick(1000);
  assert.equal(f.shown.length, 2);
  assert.equal(f.shown[0].messageKey, 'toast.missedRunFinished');
  assert.equal(f.shown[1].messageKey, 'toast.runFinished');
});

test('remembering the visible chat suppresses catch-up; a resumed outcome can still change', (t) => {
  const f = fixture(t);
  f.remember(run('a', 'failed'));
  assert.equal(f.add([run('a', 'failed')], { missed: true }), false);
  assert.equal(f.add([run('a', 'completed')]), true);
  f.tick(1000);
  assert.equal(f.shown[0].messageKey, 'toast.runFinished');
});

test('cancelled/interrupted runs are quiet; old payloads without IDs still report', (t) => {
  const f = fixture(t);
  assert.equal(f.add([run('a', 'cancelled'), run('b', 'interrupted')]), false);
  f.tick(1000);
  assert.equal(f.shown.length, 0);
  f.add([run(undefined, 'failed')]);
  f.tick(1000);
  assert.equal(f.shown[0].messageKey, 'toast.runFailed');
});

test('seen run memory is bounded for a long-lived tab', (t) => {
  const f = fixture(t);
  for (let i = 0; i < 300; i++) f.remember(run(`r${i}`));
  assert.equal(f.remember(run('r299')), false);
  assert.equal(f.remember(run('r0')), true);
});
