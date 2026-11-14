// The stale-build probe: only a vanished asset prefix counts, and it fires once.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createBuildCheck } from '../../../lovia/web/static/js/build-check.js';

function probe(responses) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init });
    const next = responses.shift();
    if (next instanceof Error) throw next;
    return { status: next };
  };
  return { fetch, calls };
}

test('a 404 on the page’s own prefix reports a new build, once', async () => {
  const { fetch, calls } = probe([200, 404, 404]);
  let stale = 0;
  const check = createBuildCheck({ base: '/static/abc123/', onStale: () => stale++, fetch });
  await check();
  assert.equal(stale, 0);
  await check();
  await check(); // already stale: no further probe
  assert.equal(stale, 1);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, '/static/abc123/favicon.svg');
  assert.deepEqual(calls[0].init, { method: 'HEAD', cache: 'no-store' });
});

test('network errors, auth failures and server errors are not a new build', async () => {
  const { fetch } = probe([new TypeError('offline'), 401, 502, 302]);
  let stale = 0;
  const check = createBuildCheck({ base: '/static/abc123/', onStale: () => stale++, fetch });
  for (let i = 0; i < 4; i++) await check();
  assert.equal(stale, 0);
});

test('overlapping probes report once', async () => {
  const { fetch } = probe([404, 404]);
  let stale = 0;
  const check = createBuildCheck({ base: '/static/abc123/', onStale: () => stale++, fetch });
  await Promise.all([check(), check()]);
  assert.equal(stale, 1);
});

test('without a static base it never probes', async () => {
  const { fetch, calls } = probe([]);
  await createBuildCheck({ base: undefined, onStale: () => assert.fail(), fetch })();
  assert.equal(calls.length, 0);
});
