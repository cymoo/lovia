// Session sidebar: list, search, switch, rename, delete, export.
import { store } from './store.js';
import { api } from './api.js';
import { promptDialog, confirmDialog } from './ui.js';
import { toast } from './toast.js';

const sessionsList = document.getElementById('sessions-list');
const chatTitleEl = document.getElementById('chat-title');
const sessionSearch = document.getElementById('session-search');
const exportBtn = document.getElementById('export-btn');

// lucide `pin` — used for the at-rest marker and the pin/unpin menu button.
const PIN_SVG =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>';

// ---- Load ----------------------------------------------------------------
export async function loadSessions(query = '') {
  try {
    const [sessions, runs] = await Promise.all([
      api.listSessions({ q: query }),
      api.listRuns().catch(() => []),
    ]);
    store.sessions = sessions;
    store.activeRuns = new Set(runs.map((r) => r.session_id));
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
    [...(store.activeRuns || [])].sort(),
    store.sessions.map((s) => [s.id, s.title ?? '', s.updated_at, s.pinned ? 1 : 0]),
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

  let prevPinned = false;
  for (const s of store.sessions) {
    const item = document.createElement('div');
    item.className = 'session-item';
    if (s.id === store.sessionId) item.classList.add('active');
    if (store.activeRuns?.has(s.id)) item.classList.add('running');
    if (s.pinned) item.classList.add('pinned');
    // Visually separate the pinned group from the rest.
    if (!s.pinned && prevPinned) item.classList.add('pin-divider');
    prevPinned = !!s.pinned;
    item.dataset.id = s.id;

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'session-main';
    main.title = s.title || s.id;
    main.innerHTML = `<div class="session-title"></div><div class="session-meta"></div>`;
    main.querySelector('.session-title').textContent = s.title || 'New chat';
    main.querySelector('.session-meta').textContent = formatTime(s.updated_at);
    main.addEventListener('click', () => switchSession(s.id));

    // At-rest pin marker (hidden on hover, where the menu takes its place).
    const pinMark = document.createElement('span');
    pinMark.className = 'session-pin';
    pinMark.setAttribute('aria-hidden', 'true');
    pinMark.innerHTML = PIN_SVG;

    const menu = document.createElement('div');
    menu.className = 'session-menu';

    const pinBtn = document.createElement('button');
    pinBtn.type = 'button';
    pinBtn.title = s.pinned ? 'Unpin' : 'Pin';
    pinBtn.innerHTML = PIN_SVG;
    if (s.pinned) pinBtn.classList.add('active');
    pinBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      togglePin(s);
    });

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

    menu.append(pinBtn, renameBtn, delBtn);
    item.append(main, pinMark, menu);
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

async function togglePin(s) {
  const next = !s.pinned;
  try {
    await api.setPinned(s.id, next);
  } catch (err) {
    console.error(err);
    toast('Couldn’t update pin', { type: 'error' });
    return;
  }
  // Update locally and re-sort to match the server's "pinned first, then most
  // recent" order — cheaper than a reload, and it keeps the active search filter.
  const target = store.sessions.find((x) => x.id === s.id);
  if (target) target.pinned = next;
  store.sessions.sort(
    (a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || b.updated_at - a.updated_at,
  );
  _lastRenderSig = null; // order changed — force a redraw
  renderSessions();
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
  if (store.sessionId === id) return;
  // Detach from any in-flight stream first — WITHOUT cancelling it. The run
  // keeps going server-side; if the session we're entering has its own live run
  // we reconnect to it below, and the one we're leaving stays reachable (its
  // sidebar dot persists) so clicking back resumes it.
  store.emit('detach-stream');
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
    if (store.sessionId !== id) return; // a newer switch superseded this one
    if (chatTitleEl) chatTitleEl.textContent = data.title || 'New chat';
    store.emit('render-history', data.entries || []);
    // Auto-reconnect when the session has an unfinished run — a page refresh
    // mid-stream, or a run left streaming when we switched away. The SSE
    // continuation streams into a new assistant bubble appended after the
    // already-rendered checkpoint history.
    if (data.active_run_id && !store.streaming) {
      store.emit('reconnect', id);
    }
  } catch (err) {
    if (store.sessionId !== id) return; // superseded; don't clobber the new view
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
