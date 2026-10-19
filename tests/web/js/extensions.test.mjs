// loadExtensions (extensions.js): deployer modules load before the app
// starts; a failure is reported, a slow one stops holding up the start.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadExtensions } from '../../../lovia/web/static/js/extensions.js';

const quiet = { error: console.error, warn: console.warn };
function silence() {
  console.error = () => {};
  console.warn = () => {};
}
function restore() {
  Object.assign(console, quiet);
}

test('every module is imported, and none failed', async () => {
  const seen = [];
  const failed = await loadExtensions(['a.js', 'b.js'], {
    importer: async (url) => seen.push(url),
  });
  assert.deepEqual(seen.sort(), ['a.js', 'b.js']);
  assert.deepEqual(failed, []);
});

test('a module that throws (or rejects) is reported, the rest still load', async () => {
  silence();
  try {
    const loaded = [];
    const failed = await loadExtensions(['bad.js', 'sync.js', 'good.js'], {
      importer: (url) => {
        if (url === 'sync.js') throw new Error('boom');
        if (url === 'bad.js') return Promise.reject(new Error('404'));
        loaded.push(url);
        return Promise.resolve();
      },
    });
    assert.deepEqual(failed.sort(), ['bad.js', 'sync.js']);
    assert.deepEqual(loaded, ['good.js']);
  } finally {
    restore();
  }
});

test('a slow module stops holding up the start, and still lands later', async () => {
  silence();
  try {
    let landed = false;
    /** @type {() => void} */
    let finish = () => {};
    const started = Date.now();
    const failed = await loadExtensions(['slow.js'], {
      timeoutMs: 30,
      importer: () =>
        new Promise((resolve) => {
          finish = () => {
            landed = true;
            resolve(undefined);
          };
        }),
    });
    assert.deepEqual(failed, []);
    assert.ok(Date.now() - started < 1000);
    assert.equal(landed, false);
    finish();
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(landed, true);
  } finally {
    restore();
  }
});

test('no modules resolves at once', async () => {
  assert.deepEqual(await loadExtensions([]), []);
});
