// The table component's pure helpers (tables.js): squaring up untrusted props,
// cell display, numeric-column detection, sorting, filtering, TSV, and the
// export snapshot.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  cellText,
  filterRows,
  normalizeTable,
  numericColumns,
  sortRows,
  tableSnapshot,
  toTsv,
} from '../../../lovia/web/static/js/tables.js';

const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });

test('normalizeTable squares rows to the columns and stringifies headings', () => {
  const t = normalizeTable({ title: 'T', columns: ['a', 2], rows: [[1], [1, 2, 3], 'x'], extra: 1 });
  assert.deepEqual(t, {
    title: 'T',
    columns: ['a', '2'],
    rows: [[1, null], [1, 2], ['x', null]],
  });
  assert.equal(normalizeTable({ columns: ['a'], rows: [] }).title, '');
});

test('normalizeTable rejects props it cannot draw', () => {
  for (const bad of [null, [], {}, { columns: [], rows: [] }, { columns: ['a'] }, { columns: ['a'], rows: {} }]) {
    assert.throws(() => normalizeTable(bad));
  }
});

test('cellText: values as given, null empty, structures as JSON', () => {
  assert.equal(cellText(2026), '2026'); // no "2,026"
  assert.equal(cellText(0.1), '0.1');
  assert.equal(cellText(false), 'false');
  assert.equal(cellText(null), '');
  assert.equal(cellText(undefined), '');
  assert.equal(cellText({ a: [1] }), '{"a":[1]}');
});

test('numericColumns: all non-null cells are numbers, and there is one', () => {
  const rows = [
    [1, 'a', null, 1],
    [2.5, 3, null, '2'],
  ];
  assert.deepEqual(numericColumns(rows, 4), [true, false, false, false]);
});

test('sortRows: numbers as numbers, strings naturally, nulls last both ways', () => {
  const rows = [[10], [null], [9], [100]];
  assert.deepEqual(sortRows(rows, 0, 1, collator), [[9], [10], [100], [null]]);
  assert.deepEqual(sortRows(rows, 0, -1, collator), [[100], [10], [9], [null]]);
  const ids = [['id-10'], ['id-9'], ['ID-1']];
  assert.deepEqual(sortRows(ids, 0, 1, collator), [['ID-1'], ['id-9'], ['id-10']]);
});

test('sortRows is stable and leaves the input alone', () => {
  const rows = [['b', 1], ['a', 2], ['b', 3], ['a', 4]];
  const copy = structuredClone(rows);
  assert.deepEqual(sortRows(rows, 0, 1, collator), [['a', 2], ['a', 4], ['b', 1], ['b', 3]]);
  assert.deepEqual(sortRows(rows, 0, -1, collator), [['b', 1], ['b', 3], ['a', 2], ['a', 4]]);
  assert.deepEqual(rows, copy);
});

test('filterRows matches any cell, case-insensitively', () => {
  const rows = [['Tokyo', 37], ['Osaka', 19], ['Kyoto', null]];
  assert.deepEqual(filterRows(rows, ' tok '), [['Tokyo', 37]]);
  assert.deepEqual(filterRows(rows, '19'), [['Osaka', 19]]);
  assert.equal(filterRows(rows, '   '), rows);
});

test('toTsv: tab-separated; tabs and newlines inside a cell become spaces', () => {
  assert.equal(toTsv([['a', 'b'], ['x\ty', 'line\nbreak'], [1, null]]), 'a\tb\nx y\tline break\n1\t');
});

test('toTsv defuses strings a spreadsheet would run as formulas', () => {
  assert.equal(toTsv([['=SUM(A1)', '@x', -5, '+1']]), "'=SUM(A1)\t'@x\t-5\t'+1");
});

test('tableSnapshot: every row, escaped, numbers right-aligned, title as caption', () => {
  const rows = Array.from({ length: 45 }, (_, i) => [`<b>${i}</b>`, i]);
  const html = String(tableSnapshot({ title: 'A & B', columns: ['name', 'n'], rows }, /** @type {any} */ ({})));
  assert.match(html, /^<figure class="lv-table"><figcaption>A &amp; B<\/figcaption><table>/);
  assert.match(html, /<th>name<\/th><th class="lv-num">n<\/th>/);
  assert.match(html, /<tr><td>&lt;b&gt;44&lt;\/b&gt;<\/td><td class="lv-num">44<\/td><\/tr>/);
  assert.equal(html.match(/<tr>/g)?.length, 46); // the heading row + all 45, unpaged
  assert.doesNotMatch(String(tableSnapshot({ columns: ['a'], rows: [] }, /** @type {any} */ ({}))), /figcaption/);
});
