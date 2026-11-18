// The follow-up on/off preference (settings.js) and the follow-up chip cache
// (followup-cache.js) must live under separate localStorage keys: they once
// shared one, so dropping a chat's chips while follow-ups were off wrote '{}'
// over '0' and silently turned the feature back on.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

const load = (name) => readFileSync(new URL(`../../../lovia/web/static/js/${name}`, import.meta.url), 'utf8')
  .replace(/^import .*;$/gm, '')
  .replace(/^export /gm, '');

// Evaluate both real modules against one shared storage, the way the page does.
function page(storage) {
  const localStorage = {
    getItem: (k) => storage.get(k) ?? null,
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  };
  const cache = {};
  runInNewContext(
    `${load('followup-cache.js')}\nObject.assign(out, { readFollowups, writeFollowups, dropFollowups });`,
    { localStorage, out: cache, Date },
  );
  const settings = {};
  runInNewContext(
    `${load('settings.js')}\nObject.assign(out, { followupsEnabled, FOLLOWUPS_KEY });`,
    { localStorage, out: settings },
  );
  return { ...cache, ...settings };
}

test('the preference and the chip cache use different keys', () => {
  const storage = new Map();
  const { writeFollowups, FOLLOWUPS_KEY } = page(storage);
  writeFollowups('s1', 'sig', ['a']);
  assert.deepEqual([...storage.keys()].filter((k) => k !== FOLLOWUPS_KEY), ['lovia-followups']);
  assert.notEqual(FOLLOWUPS_KEY, 'lovia-followups');
});

test('dropping a chat while follow-ups are off keeps them off', () => {
  const storage = new Map();
  const { writeFollowups, dropFollowups, readFollowups, followupsEnabled, FOLLOWUPS_KEY } = page(storage);
  writeFollowups('s1', 'sig', ['a', 'b']);
  writeFollowups('s2', 'sig', ['c']);
  storage.set(FOLLOWUPS_KEY, '0'); // the Settings toggle, switched off
  dropFollowups('s1');
  assert.equal(followupsEnabled(), false);
  assert.equal(readFollowups('s1', 'sig'), null);
  assert.deepEqual([...readFollowups('s2', 'sig')], ['c']); // copy out of the vm realm
});

test('switching follow-ups back on keeps the cached chips', () => {
  const storage = new Map([['lovia-followups-on', '0']]);
  const { writeFollowups, readFollowups, followupsEnabled, FOLLOWUPS_KEY } = page(storage);
  writeFollowups('s1', 'sig', ['a']);
  storage.set(FOLLOWUPS_KEY, '1');
  assert.equal(followupsEnabled(), true);
  assert.deepEqual([...readFollowups('s1', 'sig')], ['a']);
});

test('an "off" saved under the old shared key is carried over once', () => {
  const storage = new Map([['lovia-followups', '0']]);
  const { followupsEnabled, writeFollowups, dropFollowups } = page(storage);
  assert.equal(followupsEnabled(), false);
  assert.equal(storage.has('lovia-followups'), false);
  writeFollowups('s1', 'sig', ['a']);
  dropFollowups('s1');
  assert.equal(followupsEnabled(), false);
  // A later load doesn't redo it: the cache now owns 'lovia-followups'.
  assert.equal(page(storage).followupsEnabled(), false);
});

test('follow-ups default to on, including after an old "on"', () => {
  assert.equal(page(new Map()).followupsEnabled(), true);
  assert.equal(page(new Map([['lovia-followups', '1']])).followupsEnabled(), true);
});
