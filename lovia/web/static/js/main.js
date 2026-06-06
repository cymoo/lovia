// Entry point — wires together all modules.
import { store } from './store.js';
import { initTheme, initSidebarToggle } from './ui.js';
import { initComposer, cancelStream, renderHistory } from './chat.js';
import { initSessions, loadSessions, clearChat } from './sessions.js';

// ---- Agent loading ------------------------------------------------------
async function loadAgents() {
  const select = document.getElementById('agent-select');
  try {
    const res = await fetch('/api/agents');
    store.agents = await res.json();
    store.agent = store.agents[0]?.name ?? null;

    if (select && store.agents.length > 1) {
      select.style.display = '';
      select.innerHTML = store.agents.map(
        a => `<option value="${a.name}">${a.name}</option>`
      ).join('');
      select.value = store.agent;
      select.addEventListener('change', () => {
        store.agent = select.value;
        clearChat();
        document.getElementById('prompt')?.focus();
      });
    } else if (select) {
      // Single agent
      select.style.display = 'none';
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

store.on('clear-chat', clearChat);

// ---- Bootstrap ----------------------------------------------------------
(async function () {
  initTheme();
  initSidebarToggle();
  initComposer();
  initSessions();
  await loadAgents();
  document.getElementById('prompt')?.focus();

  store.on('cancel', cancelStream);
})();
