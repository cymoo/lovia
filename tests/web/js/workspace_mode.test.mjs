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

test('a switch says when it reaches the reply in flight', async () => {
  let rev = 0;
  const toasts = [];
  const store = {
    serverInfo: { workspace_modes: { modes, default: 'coding', own: null } },
    sessionId: 's',
    agents: [],
    agent: null,
    on: () => {},
    activeRuns: new Set(),
    runsBySession: new Map(),
    streaming: false,
  };
  const context = {
    store,
    t: (key) => key,
    icon: () => '',
    toast: (text) => toasts.push(text),
    api: {
      setSessionMode: async (_id, mode) => ({ workspace_mode: mode, workspace_mode_rev: ++rev }),
    },
  };
  runInNewContext(`${source}\nglobalThis.pick = pick;`, context);
  const live = (run) => {
    store.activeRuns = new Set(['s']);
    store.runsBySession = run ? new Map([['s', { run_id: run }]]) : new Map();
  };

  await context.pick('readonly');
  assert.equal(toasts.at(-1), 'mode.switched'); // nothing in flight
  // A reply that began read-only has no tools that edit or run commands —
  // through every switch it sees, its id arriving midway included.
  live(undefined);
  await context.pick('coding');
  assert.equal(toasts.at(-1), 'mode.toolsNextRun');
  live('r1');
  await context.pick('trusted');
  assert.equal(toasts.at(-1), 'mode.toolsNextRun');
  // The next reply began in full access: a switch reaches it at once.
  live('r2');
  await context.pick('coding');
  assert.equal(toasts.at(-1), 'mode.switchedLive');
});
