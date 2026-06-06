// Session sidebar: list, search, switch, rename, delete, export.
import { store } from './store.js';
import { promptDialog, confirmDialog } from './ui.js';

const sessionsList = document.getElementById('sessions-list');
const chatTitleEl = document.getElementById('chat-title');
const sessionSearch = document.getElementById('session-search');
const exportBtn = document.getElementById('export-btn');

// ---- Load ----------------------------------------------------------------
export async function loadSessions(query = '') {
  try {
    const url = query
      ? `/api/sessions?q=${encodeURIComponent(query)}`
      : '/api/sessions';
    const res = await fetch(url);
    store.sessions = await res.json();
    renderSessions();
  } catch (err) {
    console.error('loadSessions:', err);
  }
}

// ---- Render --------------------------------------------------------------
function renderSessions() {
  if (!sessionsList) return;
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
    await fetch(`/api/sessions/${s.id}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ title }),
    });
    updateSessionInSidebar(s.id, title);
  } catch (err) {
    console.error(err);
  }
}

export async function deleteSession(id) {
  const ok = await confirmDialog('Delete this chat?');
  if (!ok) return;
  try {
    await fetch(`/api/sessions/${id}`, { method: 'DELETE' });
  } catch (err) {
    console.error(err);
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
  store.emit('session-switched', id);

  const transcript = document.getElementById('transcript');
  const emptyState = document.getElementById('empty-state');
  if (emptyState) emptyState.remove();

  transcript.innerHTML = '<div class="loading">Loading…</div>';
  renderSessions();

  try {
    const res = await fetch(`/api/sessions/${id}`);
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    if (chatTitleEl) chatTitleEl.textContent = data.title || 'New chat';
    store.emit('render-history', data.entries || []);
  } catch (err) {
    transcript.innerHTML = `<div class="empty-state"><h2>Couldn't load chat</h2><p>${err.message ?? err}</p></div>`;
  }
}

export function clearChat() {
  store.sessionId = null;
  store.bubble = null;
  store.body = null;
  store.rawText = '';
  store.toolNodes.clear();
  store.lastMessage = null;
  if (chatTitleEl) chatTitleEl.textContent = 'New chat';
  if (exportBtn) exportBtn.style.display = 'none';

  const transcript = document.getElementById('transcript');
  if (transcript) {
    transcript.innerHTML = `
      <div class="empty-state" id="empty-state">
        <h2>How can I help?</h2>
        <p>Ask anything — I'll respond with tools and reasoning as needed.</p>
      </div>`;
  }
  renderSessions();
}

// ---- Export --------------------------------------------------------------
export async function exportSession(format = 'md') {
  if (!store.sessionId) return;
  try {
    const res = await fetch(`/api/sessions/${store.sessionId}/export?format=${format}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `lovia-chat.${format}`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    console.error('export:', err);
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

// Expose for title SSE event
export { updateSessionInSidebar };

// ---- Init ----------------------------------------------------------------
export function initSessions() {
  loadSessions();
  initSearch();

  document.getElementById('new-chat')?.addEventListener('click', () => {
    clearChat();
    document.getElementById('prompt')?.focus();
  });

  exportBtn?.addEventListener('click', () => exportSession('md'));

  // Listen for clear-chat from main
  store.on('clear-chat', clearChat);
}
