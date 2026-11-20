// desktop.js: when "Show in Finder" / "Open in editor" are offered, and what
// they ask the server for.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../../../lovia/web/static/js/desktop.js', import.meta.url), 'utf8')
  .replace(/^import .*;$/gm, '')
  .replace(/^export /gm, '');

function load(desktop) {
  const calls = [];
  const context = {
    store: { serverInfo: { desktop }, agent: 'bot' },
    t: (key) => key,
    icon: () => '',
    toast: () => {},
    api: { workspaceDesktop: async (...args) => void calls.push(args) },
  };
  runInNewContext(`${source}\nglobalThis.items = desktopItems;`, context);
  return { items: context.items, calls };
}

// Built in this realm: the module's arrays aren't deepEqual to ours.
const labels = (items) => Array.from(items, (i) => i.label);

test('nothing where the server offers no desktop actions, or for a remote root', () => {
  assert.deepEqual(labels(load(null).items({ path: 'a.py', isDir: false })), []);
  const { items } = load({ reveal: 'finder', edit: true });
  assert.deepEqual(labels(items({ path: 'a.py', isDir: false, remote: true })), []);
});

test('a file gets both, a folder only the file manager, named for the platform', () => {
  const mac = load({ reveal: 'finder', edit: true }).items;
  assert.deepEqual(labels(mac({ path: 'a.py', isDir: false })), ['desktop.reveal.finder', 'desktop.edit']);
  assert.deepEqual(labels(mac({ path: 'src', isDir: true })), ['desktop.reveal.finder']);
  const linux = load({ reveal: 'folder', edit: false }).items;
  assert.deepEqual(labels(linux({ path: 'a.py', isDir: false })), ['desktop.reveal.folder']);
});

test('a sidebar project names its own root', () => {
  const { items, calls } = load({ reveal: 'explorer', edit: true });
  items({ path: '.', isDir: true, root: 'C:\\work\\proj' })[0].onSelect();
  items({ path: 'src/a.py', isDir: false })[1].onSelect();
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    ['reveal', { agent: 'bot', path: '.', root: 'C:\\work\\proj' }],
    ['edit', { agent: 'bot', path: 'src/a.py' }],
  ]);
});
