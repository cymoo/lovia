// configureApi (api.js): a mount prefix reaches every URL the client builds,
// and a burst of 401s reaches the handler once.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { api, configureApi } from '../../../lovia/web/static/js/api.js';

const requested = [];
let status = 200;
let body = [];
globalThis.fetch = async (url) => {
  requested.push(url);
  return new Response(JSON.stringify(body), { status });
};

// Tests share the module's state, so they run in order: base first, then
// the one-shot 401 handler.

test('the base prefixes fetched and built URLs, trailing slash dropped', async () => {
  configureApi({ base: '/lovia/' });
  await api.listAgents();
  assert.equal(requested.at(-1), '/lovia/api/agents');
  assert.equal(api.eventsUrl(), '/lovia/api/events');
  assert.equal(api.exportUrl('s1', 'json'), '/lovia/api/sessions/s1/export?format=json');
  assert.equal(api.workspaceRawUrl({ path: 'a.png' }), '/lovia/api/workspace/raw?path=a.png');
  assert.equal(api.toolImageUrl('s1', 'c1', 0), '/lovia/api/sessions/s1/tool-images/c1/0');
});

test('other failures leave the handler alone', async () => {
  const seen = [];
  configureApi({ onUnauthorized: (err) => seen.push(err) });
  status = 403;
  body = { detail: { code: 'path_denied', message: 'no' } };
  await assert.rejects(api.listAgents(), { status: 403 });
  assert.equal(seen.length, 0);
});

test('a burst of 401s calls the handler once, with the error code', async () => {
  const seen = [];
  configureApi({ onUnauthorized: (err) => seen.push(err) });
  status = 401;
  body = { detail: { code: 'server_token', message: 'missing or invalid server token' } };
  const calls = [api.listAgents(), api.info(), api.listSessions()];
  for (const call of calls) await assert.rejects(call, { status: 401 });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].code, 'server_token');
  // Raw-Response methods go through the same gate.
  assert.equal((await api.cancel('s1')).status, 401);
  assert.equal(seen.length, 1);
});
