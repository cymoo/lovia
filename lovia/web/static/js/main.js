// Entry point — wires together all modules.
import { applyStaticI18n, currentLang, t } from './i18n.js';
import { store } from './store.js';
import { api, configureApi } from './api.js';
import { closeSidebar, initTheme, initSidebarToggle, menuOpen, promptDialog, showDialog } from './ui.js';
import {
  cancelStream,
  detachStream,
  initComposer,
  renderEmptyState,
  renderHistory,
  resetChatForNewSession,
  runReconnect,
  sendText,
  setDraft,
} from './chat.js';
import {
  initSessions,
  initEventStream,
  loadSessions,
  clearChat,
  startNewChat,
  switchSession,
} from './sessions.js';
import { initSchedules } from './schedules.js';
import { initFiles } from './files.js';
import { initMemory } from './memory.js';
import { initModelConfig } from './model-config.js';
import { initSettings } from './settings.js';
import { toast } from './toast.js';
import { loadExtensions } from './extensions.js';
import { configureLovia, emitLovia, lovia, markReady } from './lovia.js';
import {
  configureViews,
  currentView,
  initViews,
  noteAwaiting,
  openViewFromURL,
} from './views.js';
import { pageConfig } from './page-config.js';
import './charts.js'; // registers the built-in `chart` UI component
import './tables.js'; // … `table`
import './html.js'; // … and `html`

// The path prefix the app is served under, and where a signed-out user logs in.
const basePath = typeof pageConfig.base_path === 'string' ? pageConfig.base_path : '';
const loginUrl = typeof pageConfig.login_url === 'string' && pageConfig.login_url
  ? pageConfig.login_url
  : null;

// ---- Auth ---------------------------------------------------------------
// The server may guard /api/* with a token (see lovia/web/auth.py). The UI
// holds it in a cookie so requests the browser makes without JS headers
// (<img> previews, download links) carry it too. Two ways in: the printed
// /?token=... link (adopted here, then stripped from the URL), or the prompt
// shown when an API call answers 401. A 401 from a custom auth= dependency
// instead sends the user to the configured login page.
const TOKEN_COOKIE = 'lovia_token';

function saveToken(token) {
  const secure = location.protocol === 'https:' ? '; Secure' : '';
  // Scoped to the mount prefix: two instances under one host keep apart.
  document.cookie =
    `${TOKEN_COOKIE}=${encodeURIComponent(token)}` +
    `; path=${basePath}/; SameSite=Strict; Max-Age=31536000${secure}`;
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

// The first 401 only (api.js dedupes a burst of them). `server_token` is the
// built-in token check; anything else is a custom auth= dependency.
function onUnauthorized(err) {
  if (err.code === 'server_token') {
    promptForToken(); // reloads on success
  } else if (loginUrl) {
    const next = encodeURIComponent(location.pathname + location.search);
    location.assign(loginUrl.replace('{next}', next));
  } else {
    toast(t('toast.signedOut'), { type: 'error' });
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

/** @returns {Promise<boolean>} false when signed out (onUnauthorized took over). */
async function loadAgents() {
  const select = /** @type {HTMLSelectElement | null} */ (
    document.getElementById('agent-select')
  );
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
      if (switcher) switcher.classList.remove('lv-hidden');
      select.addEventListener('change', () => {
        store.agent = select.value;
        syncAgentTooltip(select);
        clearChat();
        store.emit('agent-changed', store.agent);
        document.getElementById('prompt')?.focus();
      });
    } else if (select) {
      if (switcher) switcher.classList.add('lv-hidden');
    }
    store.emit('agents-loaded', store.agents);
  } catch (err) {
    if (err.status === 401) return false;
    console.error('loadAgents:', err);
    toast(t('toast.loadAgentsFailed'), { type: 'error' });
  }
  return true;
}

// A chat opened from the sidebar belongs to the agent it was created with —
// reflect that in the switcher (without the clearChat a manual switch does)
// so follow-up messages run on, and the panels reflect, the right agent.
store.on('sync-agent', (name) => {
  if (!name || name === store.agent) return;
  if (!store.agents.some((a) => a.name === name)) return; // no longer served
  store.agent = name;
  const select = /** @type {HTMLSelectElement | null} */ (
    document.getElementById('agent-select')
  );
  if (select) {
    select.value = name;
    syncAgentTooltip(select);
  }
  store.emit('agent-changed', name);
});

// ---- Cross-module events ------------------------------------------------
store.on('render-history', (entries) => renderHistory(entries));

store.on('retry', () => {
  const promptEl = /** @type {HTMLTextAreaElement | null} */ (
    document.getElementById('prompt')
  );
  if (store.lastMessage && promptEl) {
    promptEl.value = store.lastMessage;
    /** @type {HTMLFormElement | null} */ (
      document.getElementById('composer')
    )?.requestSubmit();
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
  body.className = 'lv-shortcuts-panel';
  const h = document.createElement('h3');
  h.textContent = t('keys.title');
  body.appendChild(h);
  const list = document.createElement('dl');
  list.className = 'lv-shortcuts-list';
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
  close.className = 'lv-btn lv-btn-ghost';
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
  // Esc stops the current run. Registered in the CAPTURE phase so anything an
  // existing bubble-phase Esc handler is about to close (context popover,
  // export menu, a popup menu, mobile sidebar, files preview) is still visibly open here and
  // wins the keypress — the run stops on the next, unclaimed Esc.
  document.addEventListener(
    'keydown',
    (e) => {
      if (e.key !== 'Escape' || e.isComposing || !store.streaming) return;
      if (currentView()) return; // the run isn't on screen; Esc belongs to the view
      if (document.querySelector('dialog[open]')) return;
      if (document.getElementById('context-popover')?.hidden === false) return;
      if (document.getElementById('export-menu')?.hidden === false) return;
      if (menuOpen()) return;
      if (document.getElementById('sidebar')?.classList.contains('lv-open')) return;
      const filesOpen = document
        .getElementById('files-panel')
        ?.classList.contains('lv-open');
      const viewerOpen = !document
        .getElementById('files-viewer')
        ?.classList.contains('lv-hidden');
      const phone = window.matchMedia('(max-width: 720px)').matches;
      // files.js consumes Esc for these: closing the preview, or (on phones)
      // the panel drawer itself.
      if (filesOpen && (viewerOpen || phone)) return;
      cancelStream();
    },
    true,
  );

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
      const search = /** @type {HTMLInputElement | null} */ (
        document.getElementById('session-search')
      );
      search?.focus();
      search?.select();
    } else if (key === 'o' && e.shiftKey) {
      e.preventDefault();
      startNewChat();
    }
  });
}

// ---- window.lovia -------------------------------------------------------
// The public API's page-side half: its state, actions, and events, relayed
// from the internal store (whose event names stay free to change).
configureLovia({
  state: () => ({
    sessionId: store.sessionId,
    agent: store.agent,
    theme: store.theme === 'dark' ? 'dark' : 'light',
    lang: currentLang(),
    busy: !!store.streaming,
    view: currentView(),
  }),
  send: (text) => void sendText(text),
  setDraft,
  newChat: startNewChat,
  openSession: (id) => switchSession(id),
  toast: (text, opts) => toast(text, opts),
});
configureViews({
  changed: (view) => emitLovia('view', { view }),
  notify: (text) => toast(text),
  shown: closeSidebar, // on a phone, the drawer would cover it
});
store.on('tool-awaiting', noteAwaiting);
store.on('session-id', (sessionId) => emitLovia('session', { sessionId }));
store.on('agent-changed', (agent) => emitLovia('agent', { agent }));
store.on('theme-changed', (theme) => emitLovia('theme', { theme }));
store.on('run-start', (payload) => emitLovia('run-start', payload));
store.on('run-end', (payload) => emitLovia('run-end', payload));

// ---- Bootstrap ----------------------------------------------------------
window.lovia = lovia;

(async function () {
  configureApi({ base: basePath, onUnauthorized });
  adoptTokenFromURL(); // before the first API call
  applyStaticI18n(); // before init code reads/sets any labels
  // Extensions register before anything renders, so the first render has them.
  await loadExtensions(pageConfig.scripts ?? [], {
    onFailure: (url) => {
      const name = url.split('/').pop() || url;
      toast(t('toast.extensionFailed', { name }), { type: 'error' });
    },
  });
  initTheme();
  initSidebarToggle();
  initComposer();
  initSessions();
  initSchedules();
  initFiles();
  initMemory();
  initSettings();
  initKeyboardShortcuts();
  initViews();
  // Fetch capabilities in parallel with the agent list, but resolve them
  // BEFORE any chat history renders — edit/regenerate affordances must not
  // flash in on servers whose store can't rewind.
  const infoPromise = api.info().catch(() => null);
  if (!(await loadAgents())) return;
  document.getElementById('prompt')?.focus();

  const info = await infoPromise;
  if (info?.features?.scheduling) {
    document.getElementById('schedules-btn')?.classList.remove('lv-hidden');
  }
  if (info) store.canRewind = !!info.features?.rewind;
  if (info) store.canSuggest = !!info.features?.followups;
  if (info) {
    store.serverInfo = info;
    store.canConfigureModels = !!info.features?.model_config;
    store.configured = info.configured !== false;
    // The model chip + first-run setup; a no-op on embedder servers.
    initModelConfig();
    // The welcome view rendered before /api/info resolved; an unconfigured
    // server replaces it with the setup hero (no session exists yet).
    if (store.canConfigureModels && !store.configured) renderEmptyState();
  }
  // Push instead of poll: the lifecycle stream replaces the sidebar's run
  // polling wherever the server (and browser) support it.
  if (info?.features?.events) initEventStream();

  // Restore session from URL query string (?session=xxx).
  // Wait for the initial session list to land so the sidebar
  // is populated before we mark one as active.
  await loadSessions();
  const sid = store.readSessionFromURL();
  if (sid) {
    await switchSession(sid).catch(() => {});
  }
  openViewFromURL(); // after the chat: switching to one closes any view
  // The start is over — the list and the URL's chat loaded or failed (each
  // failure surfaces on its own, and the sidebar retries).
  markReady();
})();
