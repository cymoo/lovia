// apiError (api.js): every error-body shape a client can meet degrades to a
// readable Error, and the API's own shape keeps its code.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { apiError } from '../../../lovia/web/static/js/api.js';

const respond = (status, body) =>
  new Response(body === undefined ? 'oops' : JSON.stringify(body), {
    status,
    statusText: 'Nope',
  });

test('the API body carries code, hint and status', async () => {
  const err = await apiError(
    respond(409, { detail: { code: 'run_active', message: 'busy', hint: 'stop it' } }),
  );
  assert.equal(err.message, 'busy — stop it');
  assert.equal(err.code, 'run_active');
  assert.equal(err.hint, 'stop it');
  assert.equal(err.status, 409);
});

test('a string detail (custom auth) is the message', async () => {
  const err = await apiError(respond(401, { detail: 'go away' }));
  assert.equal(err.message, 'go away');
  assert.equal(err.code, undefined);
  assert.equal(err.status, 401);
});

test("FastAPI's validation list joins its messages", async () => {
  const err = await apiError(
    respond(422, { detail: [{ msg: 'field required' }, { msg: 'too long' }] }),
  );
  assert.equal(err.message, 'field required; too long');
});

test('a non-JSON body falls back to the status line', async () => {
  const err = await apiError(respond(502));
  assert.equal(err.message, '502 Nope');
  assert.equal(err.status, 502);
});
