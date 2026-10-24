// Which answer a run record's duration goes on (util.js lastAnswerOfRun): one
// record per supervised run, so an auto-chained run is timed as a whole.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { lastAnswerOfRun, runStamps } from '../../../lovia/web/static/js/util.js';

const user = (run) => ({ user: true, run });
const answer = { user: false };
const record = (run_id, sec, status = 'completed') =>
  ({ run_id, status, started_at: 1000, finished_at: 1000 + sec });

test('each recorded run ends on its own last answer', () => {
  const turns = [user('a'), answer, answer, user('b'), answer];
  assert.deepEqual(lastAnswerOfRun(turns, new Set(['a', 'b'])), new Map([['a', 2], ['b', 4]]));
});

test('a mid-run injected turn stays in its run', () => {
  const turns = [user('a'), answer, user('a'), answer];
  assert.deepEqual(lastAnswerOfRun(turns, new Set(['a'])), new Map([['a', 3]]));
});

test('an auto-chained leg (no record of its own) extends the run before it', () => {
  // Leg 1 is segment `a` (the record's key); leg 2 is segment `a2`, unrecorded.
  const turns = [user('a'), answer, user('a2'), answer, user('b'), answer];
  assert.deepEqual(lastAnswerOfRun(turns, new Set(['a', 'b'])), new Map([['a', 3], ['b', 5]]));
});

test('records older than the transcript misread a new run as a chained leg', () => {
  // The premise chat.js keeps by fetching records after every rebuild: with
  // run `b` missing from stale records, `a`'s duration would land on `b`'s answer.
  const turns = [user('a'), answer, user('b'), answer];
  assert.deepEqual(lastAnswerOfRun(turns, new Set(['a'])), new Map([['a', 3]]));
  assert.deepEqual(lastAnswerOfRun(turns, new Set(['a', 'b'])), new Map([['a', 1], ['b', 3]]));
});

test('segments before any record, and turns without a ref, go unstamped', () => {
  const old = [user('x'), answer, user('a'), answer];
  assert.deepEqual(lastAnswerOfRun(old, new Set(['a'])), new Map([['a', 3]]));
  const noRefs = [user(null), answer, user(null), answer];
  assert.deepEqual(lastAnswerOfRun(noRefs, new Set(['a'])), new Map());
  // A live run's turns have no ref yet: they don't extend the finished run.
  const live = [user('a'), answer, user(null), answer];
  assert.deepEqual(lastAnswerOfRun(live, new Set(['a'])), new Map([['a', 1]]));
});

test('stamps go on answered runs only', async () => {
  const turns = [user('a'), answer, user('b'), answer, user('c'), answer];
  const { stamps } = await runStamps(turns, async () => [
    record('c', 30), record('b', 5, 'failed'), record('a', 20),
  ]);
  assert.deepEqual(stamps.map((s) => [s.index, s.record.run_id]), [[1, 'a'], [5, 'c']]);
});

test('a run that finishes while the fetch is out is not in its view', async () => {
  // The transcript holds run `a` when the fetch goes out; the server's answer
  // knows only `a`. Meanwhile run `b` finishes live: its turns are drawn and
  // its ref arrives. The response must not read `b` as a chained leg of `a`.
  const turns = [user('a'), answer];
  let respond;
  const pending = runStamps(turns, () => new Promise((r) => { respond = r; }));
  turns.push(user(null), answer);
  turns[2].run = 'b';
  respond([record('a', 20)]);
  const { stamps } = await pending;
  assert.deepEqual(stamps.map((s) => [s.index, s.record.run_id]), [[1, 'a']]);
});
