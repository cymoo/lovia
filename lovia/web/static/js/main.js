// Entry point — wires together all modules.
import { applyStaticI18n, t } from './i18n.js';
import { store } from './store.js';
import { api } from './api.js';
import { initTheme, initSidebarToggle, promptDialog, showDialog } from './ui.js';
import { initComposer, detachStream, renderHistory, resetChatForNewSession, runReconnect } from './chat.js';
import { initSessions, loadSessions, clearChat, switchSession } from './sessions.js';
import { initSchedules } from './schedules.js';
import { initFiles } from './files.js';
import { initMemory } from './memory.js';
import { initSettings } from './settings.js';
import { toast } from './toast.js';

// ---- Auth ---------------------------------------------------------------
// The server may guard /api/* with a token (see lovia/web/auth.py). The UI
// holds it in a cookie so requests the browser makes without JS headers
// (<img> previews, download links) carry it too. Two ways in: the printed
// /?token=... link (adopted here, then stripped from the URL), or the prompt
// shown when the first API call answers 401.
const TOKEN_COOKIE = 'lovia_token';

function saveToken(token) {
  const secure = location.protocol === 'https:' ? '; Secure' : '';
  document.cookie =
    `${TOKEN_COOKIE}=${encodeURIComponent(token)}` +
    `; path=/; SameSite=Strict; Max-Age=31536000${secure}`;
}

function adoptTokenFromURL() {
  const url = new URL(location.href);
  if (!url.searchParams.has('token')) return;
  const token = (url.searchParams.get('token') || '').trim();
  if (token) saveToken(token); // a blank param is stripped, never stored
  url.searchParams.delete('token'); // keep the credential out of the URL bar
  history.replaceState({}, '', url);
}

async function promptForToken() {
  const token = await promptDialog(t('auth.tokenPrompt'));
  if (!token) {
    toast(t('toast.unauthorized'), { type: 'error' });
    return;
  }
  saveToken(token);
  location.reload();
}

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
    if (Array.isArray(cfg.empty_examples)) store.emptyExamples = cfg.empty_examples;
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
    if (err.status === 401) {
      await promptForToken(); // reloads on success
      return;
    }
    console.error('loadAgents:', err);
    toast(t('toast.loadAgentsFailed'), { type: 'error' });
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
function openShortcutHelp() {
  const mod = /mac/i.test(navigator.platform) ? '⌘' : 'Ctrl';
  const rows = [
    ['Enter', t('keys.send')],
    ['Shift + Enter', t('keys.newline')],
    [`${mod} + K`, t('keys.filter')],
    [`${mod} + Shift + O`, t('keys.newChat')],
    ['Esc', t('keys.esc')],
    ['?', t('keys.help')],
  ];
  const body = document.createElement('div');
  body.className = 'shortcuts-panel';
  const h = document.createElement('h3');
  h.textContent = t('keys.title');
  body.appendChild(h);
  const list = document.createElement('dl');
  list.className = 'shortcuts-list';
  for (const [keys, what] of rows) {
    const dt = document.createElement('dt');
    const kbd = document.createElement('kbd');
    kbd.textContent = keys;
    dt.appendChild(kbd);
    const dd = document.createElement('dd');
    dd.textContent = what;
    list.append(dt, dd);
  }
  body.appendChild(list);
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'btn btn-ghost';
  close.textContent = t('dialog.close');
  const dialog = showDialog({ body, actions: close });
  close.addEventListener('click', () => dialog.close());
}

function isTyping(target) {
  return (
    target instanceof HTMLElement &&
    (target.closest('input, textarea, select') || target.isContentEditable)
  );
}

function initKeyboardShortcuts() {
  document.addEventListener('keydown', (e) => {
    // `?` opens the shortcut help — but never while typing a message.
    if (e.key === '?' && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (isTyping(e.target)) return;
      e.preventDefault();
      openShortcutHelp();
      return;
    }
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
  adoptTokenFromURL(); // before the first API call
  applyStaticI18n(); // before init code reads/sets any labels
  loadPageConfig();
  initTheme();
  initSidebarToggle();
  initComposer();
  initSessions();
  initSchedules();
  initFiles();
  initMemory();
  initSettings();
  initKeyboardShortcuts();
  await loadAgents();
  document.getElementById('prompt')?.focus();

  // Reveal the schedules button only when the server advertises the feature.
  api.info()
    .then((info) => {
      if (info?.features?.scheduling) {
        document.getElementById('schedules-btn')?.classList.remove('hidden');
      }
      store.canRewind = !!info?.features?.rewind;
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
