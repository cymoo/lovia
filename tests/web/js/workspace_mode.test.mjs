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

function load(info, agents = [], agent = null) {
  const context = {
    store: {
      serverInfo: { workspace_modes: info },
      sessionId: null,
      agents,
      agent,
      on: () => {},
    },
    t: (key) => key,
    icon: () => '',
    toast: () => {},
    api: {},
  };
  runInNewContext(
    `${source}\nglobalThis.label = label;\nglobalThis.onAgents = onAgents;` +
      `\nglobalThis.draft = () => draftWorkspaceMode();`,
    context,
  );
  return context;
}

const modes = ['readonly', 'coding', 'trusted'];

test('a chat with no mode of its own is named by its agent’s mode', () => {
  const agents = [
    { name: 'lovia', mode: 'coding' },
    { name: 'review', mode: 'readonly' },
  ];
  const c = load({ modes, default: 'coding', own: 'coding' }, agents, 'review');
  assert.equal(c.label(null), 'cfg.modeReadonly');
  // Settings changes that agent's mode: the agent those chats run on is
  // rebuilt in it, so their label follows once the list is refetched.
  agents[1].mode = 'trusted';
  c.onAgents();
  assert.equal(c.label(null), 'cfg.modeTrusted');
});

test('a new chat starts in its agent’s mode', () => {
  const agents = [{ name: 'review', mode: 'readonly' }];
  const c = load({ modes, default: 'coding', own: 'coding' }, agents, 'review');
  c.onAgents();
  assert.equal(c.draft(), 'readonly');
});

test('without a mode from the agent or the server (an embedder), it stays "Default"', () => {
  const c = load({ modes: ['readonly', 'coding'], default: 'coding', own: null }, [{ name: 'bot' }], 'bot');
  assert.equal(c.label(null), 'mode.default');
});
