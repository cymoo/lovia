// Entry point — wires together all modules.
import { store } from './store.js';
import { api } from './api.js';
import { initTheme, initSidebarToggle } from './ui.js';
import { initComposer, cancelStream, renderHistory, resetChatForNewSession, runReconnect } from './chat.js';
import { initSessions, loadSessions, clearChat, switchSession } from './sessions.js';

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
async function loadAgents() {
  const select = document.getElementById('agent-select');
  const switcher = document.getElementById('agent-switcher');
  try {
    store.agents = await api.listAgents();
    store.agent = store.agents[0]?.name ?? null;

    if (select && store.agents.length > 1) {
      select.style.display = '';
      select.innerHTML = store.agents.map(
        a => `<option value="${a.name}">${a.name}</option>`
      ).join('');
      select.value = store.agent;
      if (switcher) switcher.classList.remove('hidden');
      select.addEventListener('change', () => {
        store.agent = select.value;
        clearChat();
        document.getElementById('prompt')?.focus();
      });
    } else if (select) {
      if (switcher) switcher.classList.add('hidden');
    }
  } catch (err) {
    console.error('loadAgents:', err);
  }
}

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

// ---- Bootstrap ----------------------------------------------------------
(async function () {
  loadPageConfig();
  initTheme();
  initSidebarToggle();
  initComposer();
  initSessions();
  await loadAgents();
  document.getElementById('prompt')?.focus();

  store.on('cancel', cancelStream);

  // Restore session from URL query string (?session=xxx).
  // Wait for the initial session list to land so the sidebar
  // is populated before we mark one as active.
  await loadSessions();
  const sid = store.readSessionFromURL();
  if (sid) {
    await switchSession(sid).catch(() => {});
  }
})();
