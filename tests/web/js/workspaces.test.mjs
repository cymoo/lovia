// workspaces.js: how the sidebar groups chats by the directory they work in,
// and util.js shortAge, the age at a grouped row's end.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

import { shortAge } from '../../../lovia/web/static/js/util.js';

const source = readFileSync(new URL('../../../lovia/web/static/js/workspaces.js', import.meta.url), 'utf8')
  .replace(/^import .*;$/gm, '')
  .replace(/^export /gm, '');

function load({ folded = [] } = {}) {
  const storage = new Map([['lovia-workspace-collapsed', JSON.stringify(folded)]]);
  const context = {
    store: { sessions: [], sessionId: null, on: () => {}, emit: () => {} },
    t: (key) => key,
    localStorage: { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v) },
    sessionStorage: { getItem: () => null, setItem: () => {} },
  };
  // Values cross the vm realm: compare them as plain JSON.
  context.plain = (v) => JSON.parse(JSON.stringify(v));
  runInNewContext(`${source}\nglobalThis.setList = (l) => { _list = l; };\nglobalThis.groups = (chats) => plain(sidebarGroups(chats));`, context);
  return context;
}

const ws = (root, kind = 'added', last_used_at = 0) => ({
  id: kind === 'added' ? root : null, root, name: root.slice(1), kind, last_used_at,
});
const chat = (id, root, updated_at = 0) => ({ id, workspace_root: root, updated_at });

test('the launch directory first, the rest by latest activity; unlinked last', () => {
  const c = load();
  c.setList({ workspaces: [ws('/home', 'default'), ws('/gen', 'general'), ws('/a', 'added', 5), ws('/b', 'added', 1)], default: '/home', can_add: true });
  const groups = c.groups([chat('1', '/b', 50), chat('2', '/home', 10), chat('3', null, 99), chat('4', '/gone', 99)]);
  assert.deepEqual(groups.map((g) => g.key), ['/home', '/b', '/a', '/gen', '\u0000unlinked']);
  assert.deepEqual(groups.map((g) => g.chats.map((s) => s.id)), [['2'], ['1'], [], [], ['3']]);
  // A chat whose directory left the list shows in no group (search finds it).
  assert.ok(!groups.some((g) => g.chats.some((s) => s.id === '4')));
});

test('one fixed directory: no groups; unlinked chats run in the agent own', () => {
  const c = load();
  c.setList({ workspaces: [ws('/home', 'default')], default: '/home', can_add: false });
  assert.equal(c.groups([chat('1', '/home'), chat('2', null)]), null);
  // Chats spread over directories (a server narrowed since) still group.
  const groups = c.groups([chat('1', '/home'), chat('2', null), chat('3', '/old')]);
  assert.deepEqual(groups.map((g) => [g.key, g.chats.map((s) => s.id)]), [['/home', ['1', '2']]]);
});

test('pinned chats lead in a group of their own, whatever their directory', () => {
  const c = load();
  c.setList({ workspaces: [ws('/home', 'default'), ws('/a')], default: '/home', can_add: true });
  const pin = (id, root) => ({ ...chat(id, root), pinned: true });
  const groups = c.groups([pin('1', '/a'), pin('2', '/gone'), chat('3', '/a'), chat('4', '/home')]);
  assert.deepEqual(groups.map((g) => [g.kind, g.chats.map((s) => s.id)]), [
    ['pinned', ['1', '2']], // even one whose directory left the list
    ['default', ['4']],
    ['added', ['3']],
  ]);
  // None pinned: no such group.
  assert.ok(!c.groups([chat('3', '/a')]).some((g) => g.kind === 'pinned'));
});

test('the way up from a typed path, with either separator', () => {
  const c = load();
  runInNewContext('globalThis.up = parentDir;', c);
  const cases = {
    '': null, '~/': null, '/': null, 'C:\\': null, // roots: nowhere to go
    '~/co': null, // still typing a name in the home directory
    '~/code/': '~/', '~/code/lov': '~/', '/srv/repos/': '/srv/',
    'C:\\Users\\me\\': 'C:\\Users\\', 'C:/Users/me/pro': 'C:/Users/',
  };
  for (const [typed, parent] of Object.entries(cases)) assert.equal(c.up(typed), parent, typed);
});

test('folding is remembered per directory', () => {
  const c = load({ folded: ['/a'] });
  c.setList({ workspaces: [ws('/home', 'default'), ws('/a')], default: '/home', can_add: true });
  const groups = c.groups([]);
  assert.deepEqual(groups.map((g) => g.collapsed), [false, true]);
});

test('shortAge: the largest whole unit', () => {
  const now = Date.UTC(2026, 10, 10, 12);
  const ago = (s) => (now - s * 1000) / 1000;
  assert.equal(shortAge(ago(20), now), 'now');
  assert.equal(shortAge(ago(5 * 60), now), '5m');
  assert.equal(shortAge(ago(3 * 3600), now), '3h');
  assert.equal(shortAge(ago(4 * 86400), now), '4d');
  assert.equal(shortAge(ago(15 * 86400), now), '2w');
  assert.equal(shortAge(ago(400 * 86400), now), '1y');
  assert.equal(shortAge(now / 1000 + 60, now), 'now'); // clock skew
});

test('a remote location is a key spelled like a URL; a path is local', () => {
  const { isRemote } = load();
  for (const key of ['ssh://box/srv/app', 'docker://dev@api/app', 'remote://far/~/app']) {
    assert.equal(isRemote(key), true, key);
  }
  for (const path of ['/srv/app', '~/app', 'C:/code', '/tmp/odd://name', 'git://host/x', '', null]) {
    assert.equal(isRemote(path), false, String(path));
  }
});

test('remoteKey: a host and a typed path make the key the server stores', () => {
  const { remoteKey } = load();
  assert.equal(remoteKey('ssh://box', '/srv/app'), 'ssh://box/srv/app');
  assert.equal(remoteKey('docker://dev@api', '/app/'), 'docker://dev@api/app/');
  // Under the home directory: the "~" travels, the host expands it.
  assert.equal(remoteKey('remote://far', '~/app'), 'remote://far/~/app');
  assert.equal(remoteKey('ssh://box/', '~'), 'ssh://box/~');
  // Not something the host can take as typed.
  for (const path of ['srv/app', './app', '', '~user/app']) {
    assert.equal(remoteKey('ssh://box', path), null, path);
  }
});
