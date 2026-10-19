// Tool views (tool-views.js): registration rules, per-field fallback to the
// '*' view, change notification, and the frozen ToolInfo a view sees.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  registerToolView,
  toolInfo,
  toolViewField,
  watchToolViews,
} from '../../../lovia/web/static/js/tool-views.js';

test('a view needs a name and at least one function field', () => {
  assert.throws(() => registerToolView('', { summary: () => 'x' }), /tool name/);
  assert.throws(() => registerToolView('t', {}), /expected \{ summary/);
  assert.throws(() => registerToolView('t', { summary: 'x' }), /expected \{ summary/);
  assert.throws(() => registerToolView('t', { render: () => {}, snapshot: 1 }), /expected/);
});

test("each field falls back to the '*' view on its own", () => {
  const anySummary = () => 'any';
  const anyRender = () => {};
  const searchSummary = () => 'search';
  registerToolView('*', { summary: anySummary, render: anyRender });
  registerToolView('search', { summary: searchSummary });
  assert.equal(toolViewField('search', 'summary'), searchSummary);
  assert.equal(toolViewField('search', 'render'), anyRender);
  assert.equal(toolViewField('other', 'summary'), anySummary);
  assert.equal(toolViewField('other', 'snapshot'), undefined);
});

test('registering notifies watchers with the name', () => {
  const seen = [];
  const stop = watchToolViews((name) => seen.push(name));
  registerToolView('fetch', { summary: () => 'f' });
  stop();
  registerToolView('fetch2', { summary: () => 'f' });
  assert.deepEqual(seen, ['fetch']);
});

test('ToolInfo parses arguments and is frozen', () => {
  const base = { id: 'c1', name: 'search', status: 'done', result: 'ok', isError: false };
  const info = toolInfo({ ...base, arguments: '{"q": "lovia"}' });
  assert.deepEqual(info.args, { q: 'lovia' });
  assert.equal(info.result, 'ok');
  assert.ok(Object.isFrozen(info));
  assert.equal(toolInfo({ ...base, arguments: '{oops' }).args, undefined);
  assert.equal(toolInfo({ ...base, arguments: undefined }).args, undefined);
});
