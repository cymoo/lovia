// Built-in tool views (builtin-views.js): the card line of lovia's own tools,
// and where they sit below the views a deployer registers.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { builtinSummary } from '../../../lovia/web/static/js/builtin-views.js';
import {
  registerToolView,
  toolLabel,
  toolViewChain,
  toolViewField,
} from '../../../lovia/web/static/js/tool-views.js';

test('a call reads as what it did and to what', () => {
  assert.equal(builtinSummary('read_file', { path: 'src/a.py' }), 'Read file src/a.py');
  assert.equal(builtinSummary('shell', { command: 'ls -la\n  | wc -l' }), '$ ls -la | wc -l');
  assert.equal(builtinSummary('web_search', { query: 'lovia' }), 'Web search “lovia”');
  assert.equal(builtinSummary('web_fetch', { url: 'https://x.dev' }), 'Web fetch https://x.dev');
  // Retired names keep their lines, so older chats read the same.
  assert.equal(builtinSummary('read_page', { url: 'https://x.dev' }), 'Web fetch https://x.dev');
  assert.equal(builtinSummary('http_request', { url: 'https://x.dev' }), 'HTTP request GET https://x.dev');
  assert.equal(builtinSummary('list_files', {}), 'List files');
  assert.equal(builtinSummary('list_files', { path: '.' }), 'List files');
  assert.equal(builtinSummary('list_files', { path: 'src' }), 'List files src');
  assert.equal(builtinSummary('now', undefined), 'Check the time');
  assert.equal(
    builtinSummary('show_chart', { option: { title: [{ text: 'Sales' }] } }),
    'Chart Sales',
  );
  assert.equal(builtinSummary('show_table', { columns: ['a'], rows: [] }), 'Table');
});

test('arguments that do not fit keep the default line', () => {
  assert.equal(builtinSummary('read_file', { file: 'a.py' }), null); // look-alike tool
  assert.equal(builtinSummary('read_file', undefined), null); // unparsable / too large
  assert.equal(builtinSummary('shell', { command: '  ' }), null);
  assert.equal(builtinSummary('my_tool', { path: 'a' }), null);
});

test('labels name a built-in tool; others keep their name', () => {
  assert.equal(toolLabel('read_file'), 'Read file');
  assert.equal(toolLabel('web_fetch'), 'Web fetch');
  assert.equal(toolLabel('http_request'), 'HTTP request');
  assert.equal(toolLabel('order_status'), 'order_status');
});

test("a deployer's view — even the '*' fallback — beats the built-in one", () => {
  const builtin = toolViewField('edit_file', 'summary');
  assert.ok(builtin);
  const any = () => null; // covers only some tools
  registerToolView('*', { summary: any });
  const own = () => 'own';
  registerToolView('edit_file', { summary: own });
  // Most specific first; a summary that returns nothing hands over to the
  // next, so a partial '*' leaves lovia's own lines in place.
  assert.deepEqual(toolViewChain('edit_file', 'summary'), [own, any, builtin]);
  assert.deepEqual(toolViewChain('write_file', 'summary').slice(0, 1), [any]);
  assert.equal(toolViewChain('write_file', 'summary').length, 2);
  assert.deepEqual(toolViewChain('order_status', 'summary'), [any]);
});
