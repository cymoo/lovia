// schedule-time.js: the form's pickers ⇄ the API's triggers, and a schedule
// in plain words.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  dailyCron,
  describeTrigger,
  displayName,
  formatWhen,
  humanizeCron,
  isDone,
  parseDaily,
  splitInterval,
  statusOf,
  unitSeconds,
} from '../../../lovia/web/static/js/schedule-time.js';

test('a daily cron reads back into days and a time', () => {
  assert.deepEqual(parseDaily('0 9 * * 1-5'), { days: [1, 2, 3, 4, 5], hour: 9, minute: 0 });
  assert.deepEqual(parseDaily('30 18 * * *'), { days: [0, 1, 2, 3, 4, 5, 6], hour: 18, minute: 30 });
  assert.deepEqual(parseDaily('5 7 * * 7,6'), { days: [0, 6], hour: 7, minute: 5 });
  // Anything else needs the raw Cron tab.
  for (const expr of ['*/5 * * * *', '0 9 1 * *', '0 9 * 1 *', '0 9,18 * * *', '0 24 * * *', 'nope']) {
    assert.equal(parseDaily(expr), null, expr);
  }
});

test('the daily picker writes ranges, lists and * as people would', () => {
  assert.equal(dailyCron({ days: [1, 2, 3, 4, 5], hour: 9, minute: 0 }), '0 9 * * 1-5');
  assert.equal(dailyCron({ days: [6, 0], hour: 9, minute: 0 }), '0 9 * * 0,6');
  assert.equal(dailyCron({ days: [0, 1, 2, 3, 4, 5, 6], hour: 7, minute: 30 }), '30 7 * * *');
  assert.equal(dailyCron({ days: [1, 2, 4], hour: 9, minute: 0 }), '0 9 * * 1,2,4');
  assert.equal(dailyCron({ days: [1, 3, 4, 5], hour: 9, minute: 0 }), '0 9 * * 1,3-5');
  for (const days of [[1], [0, 6], [1, 2, 3], [0, 2, 4, 6]]) {
    const daily = { days, hour: 8, minute: 15 };
    assert.deepEqual(parseDaily(dailyCron(daily)), daily);
  }
});

test('an interval takes its largest whole unit', () => {
  assert.deepEqual(splitInterval(1800), { n: 30, unit: 'minute' });
  assert.deepEqual(splitInterval(7200), { n: 2, unit: 'hour' });
  assert.deepEqual(splitInterval(86400 * 3), { n: 3, unit: 'day' });
  assert.deepEqual(splitInterval(45), { n: 45, unit: 'second' });
  assert.deepEqual(splitInterval(90), { n: 90, unit: 'second' });
  assert.equal(unitSeconds('hour'), 3600);
  assert.equal(unitSeconds('second'), 1);
});

test('cron in plain words', () => {
  assert.equal(humanizeCron('0 9 * * 1-5'), 'Weekdays at 09:00');
  assert.equal(humanizeCron('0 9 * * *'), 'Every day at 09:00');
  assert.equal(humanizeCron('0 10 * * 0,6'), 'Weekends at 10:00');
  assert.equal(humanizeCron('30 10 * * 1,3'), 'Mon, Wed at 10:30');
  assert.equal(humanizeCron('*/15 * * * *'), 'Every 15 min');
  assert.equal(humanizeCron('0 9 1 * *'), 'Monthly on day 1 at 09:00');
  assert.equal(humanizeCron('0 9 * 1 *'), null);
});

test('a moment is as near as worth saying', () => {
  const now = new Date(2026, 10, 14, 10, 0).getTime(); // a Saturday
  const at = (/** @type {number[]} */ ...parts) => new Date(...parts).getTime() / 1000;
  assert.equal(formatWhen(at(2026, 10, 14, 12, 43), now), 'today 12:43');
  assert.equal(formatWhen(at(2026, 10, 15, 9, 0), now), 'tomorrow 09:00');
  assert.equal(formatWhen(at(2026, 10, 13, 9, 0), now), 'yesterday 09:00');
  assert.equal(formatWhen(at(2026, 10, 17, 9, 0), now), 'Tue 09:00');
});

test('a trigger and a name for the list', () => {
  const base = { id: 's', input: 'x', active: true, next_fire: 0 };
  assert.equal(describeTrigger({ ...base, trigger_kind: 'every', trigger_expr: '1800' }), 'Every 30 min');
  assert.equal(describeTrigger({ ...base, trigger_kind: 'cron', trigger_expr: '0 9 * * 1-5' }), 'Weekdays at 09:00');
  assert.equal(describeTrigger({ ...base, trigger_kind: 'cron', trigger_expr: '0 9 * 1 *' }), 'cron 0 9 * 1 *');
  const s = { ...base, trigger_kind: 'every', trigger_expr: '60' };
  assert.equal(displayName({ ...s, name: 'Digest' }), 'Digest');
  assert.equal(displayName({ ...s, input: '\n  First line\nsecond' }), 'First line');
});

test('paused, done and running are told apart', () => {
  const now = Date.now();
  const s = { id: 's', input: 'x', trigger_kind: 'every', trigger_expr: '60', next_fire: 0 };
  assert.equal(statusOf({ ...s, active: true }, new Set()), 'active');
  assert.equal(statusOf({ ...s, active: true }, new Set(['s'])), 'running');
  assert.equal(statusOf({ ...s, active: false }, new Set()), 'paused');
  assert.equal(statusOf({ ...s, active: false, finished_reason: 'expired' }, new Set()), 'done');
  const past = { ...s, active: false, trigger_kind: 'at', trigger_expr: String(now / 1000 - 60) };
  assert.equal(isDone(past, now), true);
  const future = { ...past, trigger_expr: String(now / 1000 + 60) };
  assert.equal(isDone(future, now), false);
});
