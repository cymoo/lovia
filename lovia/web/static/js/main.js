// Entry point — wires together all modules.
import { store } from './store.js';
import { api } from './api.js';
import { initTheme, initSidebarToggle } from './ui.js';
import { initComposer, detachStream, renderHistory, resetChatForNewSession, runReconnect } from './chat.js';
import { initSessions, loadSessions, clearChat, switchSession } from './sessions.js';
import { initSchedules } from './schedules.js';
import { initFiles } from './files.js';
import { initMemory } from './memory.js';
import { toast } from './toast.js';

// ---- Page config --------------------------------------------------------
function loadPageConfig() {
  const node = document.getElementById('app-config');
  if (!node?.textContent) return;
  try {
    const cfg = JSON.parse(node.textContent);
    if (typeof cfg.empty_title === 'string') store.emptyTitle = cfg.empty_title;
    if (typeof cfg.empty_description === 'string' || Array.isArray(cfg.empty_description)) {
      store.emptyDescription = cfg.empty_description;
    }
  } catch (err) {
    console.error('app-config:', err);
  }
}

// ---- Agent loading ------------------------------------------------------
// First instruction line as a hover hint — the switcher shows bare names, so
// this is the only in-UI answer to "which agent does what".
function agentHint(agent) {
  const line = (agent?.instructions || '').split('\n', 1)[0].trim();
  return line.length > 160 ? `${line.slice(0, 159)}…` : line;
}

function syncAgentTooltip(select) {
  select.title = agentHint(store.agents.find((a) => a.name === store.agent));
}

async function loadAgents() {
  const select = document.getElementById('agent-select');
  const switcher = document.getElementById('agent-switcher');
  try {
    store.agents = await api.listAgents();
    store.agent = store.agents[0]?.name ?? null;

    if (select && store.agents.length > 1) {
      select.style.display = '';
      select.replaceChildren(...store.agents.map((a) => {
        const opt = document.createElement('option');
        opt.value = a.name;
        opt.textContent = a.name;
        opt.title = agentHint(a);
        return opt;
      }));
      select.value = store.agent;
      syncAgentTooltip(select);
      if (switcher) switcher.classList.remove('hidden');
      select.addEventListener('change', () => {
        store.agent = select.value;
        syncAgentTooltip(select);
        clearChat();
        store.emit('agent-changed', store.agent);
        document.getElementById('prompt')?.focus();
      });
    } else if (select) {
      if (switcher) switcher.classList.add('hidden');
    }
    store.emit('agents-loaded', store.agents);
  } catch (err) {
    console.error('loadAgents:', err);
    toast('Couldn’t load agents', { type: 'error' });
  }
}

// A chat opened from the sidebar belongs to the agent it was created with —
// reflect that in the switcher (without the clearChat a manual switch does)
// so follow-up messages run on, and the panels reflect, the right agent.
store.on('sync-agent', (name) => {
  if (!name || name === store.agent) return;
  if (!store.agents.some((a) => a.name === name)) return; // no longer served
  store.agent = name;
  const select = document.getElementById('agent-select');
  if (select) {
    select.value = name;
    syncAgentTooltip(select);
  }
  store.emit('agent-changed', name);
});

// ---- Cross-module events ------------------------------------------------
store.on('render-history', (entries) => renderHistory(entries));

store.on('retry', () => {
  const promptEl = document.getElementById('prompt');
  if (store.lastMessage && promptEl) {
    promptEl.value = store.lastMessage;
    document.getElementById('composer')?.requestSubmit();
  }
});

store.on('reset-chat-view', resetChatForNewSession);

store.on('reconnect', (sessionId) => {
  if (!store.streaming) runReconnect(sessionId);
});

// Switching sessions / starting a new chat detaches the live stream without
// cancelling it (the run keeps going server-side and can be reconnected).
store.on('detach-stream', detachStream);

// ---- Keyboard shortcuts -------------------------------------------------
function initKeyboardShortcuts() {
  document.addEventListener('keydown', (e) => {
    if (!(e.metaKey || e.ctrlKey)) return;
    const key = e.key.toLowerCase();
    if (key === 'k') {
      e.preventDefault(); // focus the chat filter
      const search = document.getElementById('session-search');
      search?.focus();
      search?.select();
    } else if (key === 'o' && e.shiftKey) {
      e.preventDefault(); // start a new chat
      clearChat();
      document.getElementById('prompt')?.focus();
    }
  });
}

// ---- Bootstrap ----------------------------------------------------------
(async function () {
  loadPageConfig();
  initTheme();
  initSidebarToggle();
  initComposer();
  initSessions();
  initSchedules();
  initFiles();
  initMemory();
  initKeyboardShortcuts();
  await loadAgents();
  document.getElementById('prompt')?.focus();

  // Reveal the schedules button only when the server advertises the feature.
  api.info()
    .then((info) => {
      if (info?.features?.scheduling) {
        document.getElementById('schedules-btn')?.classList.remove('hidden');
      }
    })
    .catch(() => {});

  // Restore session from URL query string (?session=xxx).
  // Wait for the initial session list to land so the sidebar
  // is populated before we mark one as active.
  await loadSessions();
  const sid = store.readSessionFromURL();
  if (sid) {
    await switchSession(sid).catch(() => {});
  }
})();
