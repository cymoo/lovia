// workspace-mode.js: what the composer's mode chip calls a chat's mode.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../../../lovia/web/static/js/workspace-mode.js', import.meta.url), 'utf8')
  .replace(/^import .*;$/gm, '')
  .replace(/^export /gm, '');

function load(info) {
  const context = {
    store: { serverInfo: { workspace_modes: info }, sessionId: 'old', on: () => {} },
    t: (key) => key,
    icon: () => '',
    toast: () => {},
    api: {},
  };
  runInNewContext(`${source}\nglobalThis.label = label;\nglobalThis.onConfig = onConfig;`, context);
  return context;
}

test('a chat with no mode of its own is named by the mode it runs in', () => {
  const c = load({ modes: ['readonly', 'coding', 'trusted'], default: 'readonly', own: 'readonly' });
  assert.equal(c.label(null), 'cfg.modeReadonly');
  // Settings changes the default: the agent those chats run on is rebuilt in
  // it, so their label follows — the chip must not keep the old one.
  c.onConfig({ workspace: { effective_mode: 'trusted' } });
  assert.equal(c.label(null), 'cfg.modeTrusted');
});

test('without a named own mode (an embedder), it stays "Default"', () => {
  const c = load({ modes: ['readonly', 'coding'], default: 'coding', own: null });
  c.onConfig({ workspace: { effective_mode: 'readonly' } });
  assert.equal(c.label(null), 'mode.default');
});
