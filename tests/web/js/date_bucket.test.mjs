// The sidebar's date groups (util.js dateBucket): local calendar days, not
// rolling 24-hour windows.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { dateBucket } from '../../../lovia/web/static/js/util.js';

const now = new Date(2026, 9, 24, 0, 30); // just past midnight, local time
const at = (...args) => new Date(...args).getTime() / 1000; // server stamps are seconds

test('calendar days, not 24-hour windows', () => {
  assert.equal(dateBucket(at(2026, 9, 24, 0, 5), now), 'today');
  // Forty minutes ago, but before midnight: yesterday.
  assert.equal(dateBucket(at(2026, 9, 23, 23, 50), now), 'yesterday');
  assert.equal(dateBucket(at(2026, 9, 23, 0, 0), now), 'yesterday');
  assert.equal(dateBucket(at(2026, 9, 22, 23, 59), now), 'week');
});

test('the 7- and 30-day edges', () => {
  assert.equal(dateBucket(at(2026, 9, 17, 0, 0), now), 'week');
  assert.equal(dateBucket(at(2026, 9, 16, 23, 59), now), 'month');
  assert.equal(dateBucket(at(2026, 8, 24, 0, 0), now), 'month');
  assert.equal(dateBucket(at(2026, 8, 23, 23, 59), now), 'older');
  assert.equal(dateBucket(at(2025, 11, 31), now), 'older');
});

test('milliseconds work too, and a future stamp is today', () => {
  assert.equal(dateBucket(new Date(2026, 9, 24, 0, 1).getTime(), now), 'today');
  assert.equal(dateBucket(at(2026, 9, 25, 9, 0), now), 'today');
});
