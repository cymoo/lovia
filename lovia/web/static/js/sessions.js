// Session sidebar: list, search, switch, rename, delete, export.
import { store } from './store.js';
import { api } from './api.js';
import { promptDialog, confirmDialog } from './ui.js';
import { toast } from './toast.js';

const sessionsList = document.getElementById('sessions-list');
const chatTitleEl = document.getElementById('chat-title');
const sessionSearch = document.getElementById('session-search');
const exportBtn = document.getElementById('export-btn');

// ---- Load ----------------------------------------------------------------
export async function loadSessions(query = '') {
  try {
    store.sessions = await api.listSessions({ q: query });
    renderSessions();
  } catch (err) {
    console.error('loadSessions:', err);
  }
}

// ---- Render --------------------------------------------------------------
// A cheap fingerprint of what renderSessions() draws, so repeated polls with
// identical data don't tear down and rebuild the whole sidebar.
let _lastRenderSig = null;
function sessionsSignature() {
  return JSON.stringify([
    store.sessionId,
    store.sessions.map((s) => [s.id, s.title ?? '', s.updated_at]),
  ]);
}

function renderSessions() {
  if (!sessionsList) return;
  const sig = sessionsSignature();
  if (sig === _lastRenderSig) return; // nothing changed — keep the DOM as-is
  _lastRenderSig = sig;
  sessionsList.innerHTML = '';

  if (!store.sessions.length) {
    const empty = document.createElement('div');
    empty.className = 'sessions-empty';
    empty.textContent = 'No chats yet.';
    sessionsList.appendChild(empty);
    return;
  }

  for (const s of store.sessions) {
    const item = document.createElement('div');
    item.className = 'session-item';
    if (s.id === store.sessionId) item.classList.add('active');
    item.dataset.id = s.id;

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'session-main';
    main.title = s.title || s.id;
    main.innerHTML = `<div class="session-title"></div><div class="session-meta"></div>`;
    main.querySelector('.session-title').textContent = s.title || 'New chat';
    main.querySelector('.session-meta').textContent = formatTime(s.updated_at);
    main.addEventListener('click', () => switchSession(s.id));

    const menu = document.createElement('div');
    menu.className = 'session-menu';

    const renameBtn = document.createElement('button');
    renameBtn.type = 'button';
    renameBtn.title = 'Rename';
    renameBtn.innerHTML = '✎';
    renameBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      renameSession(s);
    });

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.title = 'Delete';
    delBtn.innerHTML = '✕';
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteSession(s.id);
    });

    menu.append(renameBtn, delBtn);
    item.append(main, menu);
    sessionsList.appendChild(item);
  }

  // Sync header
  if (store.sessionId) {
    const active = store.sessions.find(s => s.id === store.sessionId);
    if (active?.title) {
      if (chatTitleEl) chatTitleEl.textContent = active.title;
    }
    if (exportBtn) exportBtn.style.display = '';
  } else {
    if (exportBtn) exportBtn.style.display = 'none';
  }
}

// ---- Update a single session's title in the sidebar --------------------
function updateSessionInSidebar(sessionId, title) {
  // Update the cached sessions list
  const s = store.sessions.find(s => s.id === sessionId);
  if (s) s.title = title;

  // Update the DOM directly without full re-render
  const item = sessionsList?.querySelector(`.session-item[data-id="${sessionId}"]`);
  if (item) {
    const titleEl = item.querySelector('.session-title');
    if (titleEl) titleEl.textContent = title || 'New chat';
  }

  // Update header if this is the active session
  if (sessionId === store.sessionId && title) {
    if (chatTitleEl) chatTitleEl.textContent = title;
  }
}

// ---- Actions -------------------------------------------------------------
async function renameSession(s) {
  const title = await promptDialog('Rename chat:', s.title || '');
  if (!title) return;
  try {
    await api.renameSession(s.id, title);
    updateSessionInSidebar(s.id, title);
  } catch (err) {
    console.error(err);
    toast('Couldn’t rename chat', { type: 'error' });
  }
}

export async function deleteSession(id) {
  const ok = await confirmDialog('Delete this chat?');
  if (!ok) return;
  try {
    await api.deleteSession(id);
  } catch (err) {
    console.error(err);
    toast('Couldn’t delete chat', { type: 'error' });
    return; // leave the view untouched if the delete didn't land
  }
  if (store.sessionId === id) {
    store.sessionId = null;
    store.emit('clear-chat');
  }
  await loadSessions();
}

export async function switchSession(id) {
  if (store.streaming || store.sessionId === id) return;
  store.sessionId = id;
  store.syncURL(id);
  store.emit('session-switched', id);

  const transcript = document.getElementById('transcript');
  const emptyState = document.getElementById('empty-state');
  if (emptyState) emptyState.remove();

  transcript.replaceChildren(
    document.getElementById('tmpl-skeleton').content.cloneNode(true),
  );
  renderSessions();

  try {
    const data = await api.getSession(id);
    if (chatTitleEl) chatTitleEl.textContent = data.title || 'New chat';
    store.emit('render-history', data.entries || []);
    // Auto-reconnect when the session has an unfinished run (e.g. page refresh
    // mid-stream). The SSE continuation streams into a new assistant bubble
    // appended after the already-rendered checkpoint history.
    if (data.active_run_id && !store.streaming) {
      store.emit('reconnect', id);
    }
  } catch (err) {
    const errState = document.createElement('div');
    errState.className = 'empty-state';
    const h2 = document.createElement('h2');
    h2.textContent = "Couldn't load chat";
    const p = document.createElement('p');
    p.textContent = err.message ?? String(err);
    errState.append(h2, p);
    transcript.replaceChildren(errState);
  }
}

export function clearChat() {
  store.sessionId = null;
  store.syncURL(null);
  store.lastMessage = null;
  if (chatTitleEl) chatTitleEl.textContent = 'New chat';
  if (exportBtn) exportBtn.style.display = 'none';
  store.emit('reset-chat-view');
  renderSessions();
}

// ---- Export --------------------------------------------------------------
export async function exportSession(format = 'md') {
  if (!store.sessionId) return;
  try {
    const res = await fetch(api.exportUrl(store.sessionId, format));
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `lovia-chat.${format}`;
    a.click();
    URL.revokeObjectURL(url);
    toast('Chat exported');
  } catch (err) {
    console.error('export:', err);
    toast('Export failed', { type: 'error' });
  }
}

// ---- Search --------------------------------------------------------------
let _searchTimer = null;
export function initSearch() {
  if (!sessionSearch) return;
  sessionSearch.addEventListener('input', () => {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => loadSessions(sessionSearch.value.trim()), 250);
  });
}

// ---- Utility -------------------------------------------------------------
function formatTime(ts) {
  if (!ts) return '';
  // Accept seconds or ms
  const ms = ts > 1e12 ? ts : ts * 1000;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return String(ts);
  // ISO format without timezone: YYYY-MM-DD HH:MM
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ---- Init ----------------------------------------------------------------
export function initSessions() {
  loadSessions();
  initSearch();

  document.getElementById('new-chat')?.addEventListener('click', () => {
    clearChat();
    document.getElementById('prompt')?.focus();
  });

  exportBtn?.addEventListener('click', () => exportSession('md'));
  store.on('clear-chat', clearChat);
}
