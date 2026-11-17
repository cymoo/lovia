import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createAttention, waitKey } from '../../../lovia/web/static/js/attention.js';

const run = (run_id, waits_on = null, session_id = 'chat') => ({ run_id, session_id, waits_on });
const card = (id) => ({ kind: 'approval', id });

test('the first snapshot only seeds: a chat already waiting is no news', () => {
  const a = createAttention();
  assert.deepEqual(a.take([run('r1', card('c1'))]), []);
  assert.deepEqual(a.take([run('r1', card('c1'))]), []);
});

test('a new wait is told once, however many snapshots show it', () => {
  const a = createAttention();
  a.take([]);
  const asked = run('r1', card('c1'));
  assert.deepEqual(a.take([run('r0'), asked]), [asked]);
  assert.deepEqual(a.take([asked]), []);
});

test('a card asked live, then suspended, is one wait', () => {
  const a = createAttention();
  a.take([]);
  assert.equal(a.take([run('r1', card('c1'))]).length, 1);
  // The same call, now waiting with the run suspended: same key.
  assert.deepEqual(a.take([run('r1', { kind: 'approval', id: 'c1' })]), []);
});

test('the next call the run waits on is news again', () => {
  const a = createAttention();
  a.take([]);
  a.take([run('r1', card('c1'))]);
  const question = run('r1', { kind: 'question', id: 'q2' });
  assert.deepEqual(a.take([question]), [question]);
});

test('a run waiting on nobody has no key', () => {
  assert.equal(waitKey(run('r1')), null);
  assert.equal(waitKey(run('r1', card('c1'))), 'r1:c1');
});
