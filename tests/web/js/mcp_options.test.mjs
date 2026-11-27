// Settings → MCP options (mcp-options.js): controls over the editor's JSON,
// read the way lovia/web/config/mcp.py reads a document.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { optionsOf, soleServer, withOption } from '../../../lovia/web/static/js/mcp-options.js';

const stdio = { command: 'npx', args: ['-y', 'server'] };
const http = { type: 'http', url: 'https://x.dev/mcp' };

test('one server, wrapped or bare; none or several is no server', () => {
  assert.deepEqual(soleServer({ mcpServers: { gh: stdio } }), stdio);
  assert.deepEqual(soleServer({ gh: stdio }), stdio);
  assert.equal(soleServer({ mcpServers: { a: stdio, b: http } }), null);
  assert.equal(soleServer({ mcpServers: {} }), null);
  // An unnamed entry is refused by the server: no options for it either.
  assert.equal(soleServer(stdio), null);
  assert.equal(soleServer({ gh: 'npx' }), null);
  assert.equal(soleServer(null), null);
  assert.equal(soleServer([stdio]), null);
});

test('options read as the server reads them', () => {
  assert.deepEqual(optionsOf(stdio), { defer: false, proxy: 'default', http: false });
  assert.deepEqual(optionsOf({ ...http, defer: true, trustEnv: false }), {
    defer: true,
    proxy: 'off',
    http: true,
  });
  assert.equal(optionsOf({ ...http, trustEnv: true }).proxy, 'on');
  // httpUrl / serverUrl are urls too.
  assert.equal(optionsOf({ httpUrl: 'https://x.dev' }).http, true);
  assert.equal(optionsOf({ serverUrl: 'https://x.dev' }).http, true);
  // Not a boolean: the control shows the default; saving says what's wrong.
  assert.equal(optionsOf({ ...stdio, defer: 'yes' }).defer, false);
});

test('a control rewrites its key; the default removes it', () => {
  const text = JSON.stringify({ mcpServers: { gh: http } });
  const on = withOption(text, 'defer', true);
  assert.deepEqual(JSON.parse(on ?? ''), { mcpServers: { gh: { ...http, defer: true } } });
  assert.deepEqual(JSON.parse(withOption(on ?? '', 'defer', false) ?? ''), {
    mcpServers: { gh: http },
  });
  const proxied = withOption(text, 'proxy', 'off');
  assert.equal(JSON.parse(proxied ?? '').mcpServers.gh.trustEnv, false);
  assert.equal('trustEnv' in JSON.parse(withOption(proxied ?? '', 'proxy', 'default') ?? '').mcpServers.gh, false);
  // A bare document stays bare; the result is pretty-printed.
  const bare = withOption(JSON.stringify({ gh: stdio }), 'defer', true);
  assert.deepEqual(JSON.parse(bare ?? ''), { gh: { ...stdio, defer: true } });
  assert.match(bare ?? '', /\n {2}"gh"/);
});

test('nothing to rewrite: not JSON, or not one server', () => {
  assert.equal(withOption('{ "gh": ', 'defer', true), null);
  assert.equal(withOption(JSON.stringify({ a: stdio, b: stdio }), 'defer', true), null);
});
