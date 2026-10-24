// Which answer a run record's duration goes on (util.js lastAnswerOfRun): one
// record per supervised run, so an auto-chained run is timed as a whole.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { lastAnswerOfRun } from '../../../lovia/web/static/js/util.js';

const user = (run) => ({ user: true, run });
const answer = { user: false };

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
