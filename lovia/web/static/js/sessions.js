// Session sidebar: list, search, switch, rename, delete, export.
import { t } from './i18n.js';
import { store } from './store.js';
import { api } from './api.js';
import { promptDialog, confirmDialog, showDialog } from './ui.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { exportSessionHtml, exportFilename } from './export.js';
import { notificationsEnabled } from './settings.js';
import { formatDateTime, formatTimeSmart } from './util.js';

const sessionsList = document.getElementById('sessions-list');
const chatTitleEl = document.getElementById('chat-title');
const sessionSearch = document.getElementById('session-search');
const exportBtn = document.getElementById('export-btn');
const exportWrap = document.getElementById('export-wrap');

// lucide `pin` — the at-rest marker and the pin/unpin menu button.
const PIN_SVG = icon('pin', { size: 14 });

// The sidebar renders at most one page of chats; anything beyond that lives in
// the "View all" dialog, which loads further pages on demand.
const PAGE_SIZE = 50;
// Whether the last load hit the cap (⇒ show the "View all" row).
let _hasMore = false;

// ---- Background-run awareness --------------------------------------------
// Completion is detected wherever the run set refreshes: a session that was
// running and no longer is has finished. Detection lives in loadSessions() so
// every refresh path benefits; the poller below just guarantees refreshes
// keep happening — at a lively cadence while the tab is visible, slowly while
// hidden (a hidden tab is exactly where "your run finished" matters most).
const POLL_VISIBLE_MS = 8000;
const POLL_HIDDEN_MS = 30000;
const STOPPED_GRACE_MS = 10000;

let _pollTimer = null;
let _runsPrimed = false; // first load only seeds the baseline — no notices
const _recentlyStopped = new Map(); // sid → ts of a UI-initiated stop
let _unseenFinished = 0; // completions while the tab was hidden
const _baseTitle = document.title;

function _notifyRunFinished(sid) {
  const stoppedAt = _recentlyStopped.get(sid);
  if (stoppedAt && Date.now() - stoppedAt < STOPPED_GRACE_MS) return;
  // The chat on screen ends its own stream visibly — no extra notice.
  if (sid === store.sessionId && store.streaming) return;
  const title = store.sessions.find((s) => s.id === sid)?.title || t('toast.backgroundRun');
  toast(t('toast.runFinished', { title }), { type: 'success' });
  if (document.hidden) {
    _unseenFinished += 1;
    document.title = `(${_unseenFinished}) ${_baseTitle}`;
    // Only while hidden — a visible tab's toast is notification enough.
    if (notificationsEnabled()) {
      try {
        new Notification(_baseTitle, { body: t('toast.runFinished', { title }) });
      } catch { /* platform quirks (e.g. no Notification in this context) */ }
    }
  }
}

function _schedulePoll() {
  clearTimeout(_pollTimer);
  _pollTimer = setTimeout(async () => {
    await loadSessions(sessionSearch?.value.trim() || '');
    _schedulePoll();
  }, document.hidden ? POLL_HIDDEN_MS : POLL_VISIBLE_MS);
}

async function stopRun(sid) {
  // Suppress the "finished" notice for a stop the user just asked for.
  _recentlyStopped.set(sid, Date.now());
  // Entries only matter within the grace window — don't let the map grow
  // for the lifetime of the tab.
  setTimeout(() => _recentlyStopped.delete(sid), STOPPED_GRACE_MS);
  try {
    await api.cancel(sid);
    toast(t('toast.runStopped'));
  } catch (err) {
    console.error('stopRun:', err);
    toast(t('toast.stopFailed'), { type: 'error' });
  }
  loadSessions(sessionSearch?.value.trim() || '');
}

// ---- Load ----------------------------------------------------------------
export async function loadSessions(query = '') {
  try {
    const [sessions, runs] = await Promise.all([
      // Fetch one row past the page: its presence answers "is there more?"
      // without a count endpoint or a response-shape change.
      api.listSessions({ q: query, limit: PAGE_SIZE + 1 }),
      api.listRuns().catch(() => []),
    ]);
    _hasMore = sessions.length > PAGE_SIZE;
    store.sessions = sessions.slice(0, PAGE_SIZE);
    const prevRuns = store.activeRuns || new Set();
    store.activeRuns = new Set(runs.map((r) => r.session_id));
    if (_runsPrimed) {
      for (const sid of prevRuns) {
        if (!store.activeRuns.has(sid)) _notifyRunFinished(sid);
      }
    }
    _runsPrimed = true;
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
    _hasMore,
    store.agents.length > 1, // agent chips appear once agents finish loading
    [...(store.activeRuns || [])].sort(),
    store.sessions.map((s) => [
      s.id, s.title ?? '', s.updated_at, s.pinned ? 1 : 0, s.agent ?? '',
    ]),
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
    empty.textContent = t('nav.noChats');
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
    main.querySelector('.session-title').textContent = s.title || t('session.newChat');
    const meta = main.querySelector('.session-meta');
    meta.textContent = formatTimeSmart(s.updated_at);
    meta.title = formatDateTime(s.updated_at);
    // Which brain a chat belongs to — only worth pixels when there's a choice.
    if (store.agents.length > 1 && s.agent) {
      const chip = document.createElement('span');
      chip.className = 'session-agent';
      chip.textContent = s.agent;
      meta.append(' · ', chip);
    }
    main.addEventListener('click', () => switchSession(s.id));

    // At-rest pin marker (hidden on hover, where the menu takes its place).
    const pinMark = document.createElement('span');
    pinMark.className = 'session-pin';
    pinMark.setAttribute('aria-hidden', 'true');
    pinMark.innerHTML = PIN_SVG;

    const menu = document.createElement('div');
    menu.className = 'session-menu';

    // A running session gets a stop control right in the sidebar — no need to
    // open the chat just to end its background run.
    if (store.activeRuns?.has(s.id)) {
      const stopBtn = document.createElement('button');
      stopBtn.type = 'button';
      stopBtn.title = t('session.stop');
      stopBtn.className = 'session-stop';
      stopBtn.innerHTML = icon('square', { size: 13 });
      stopBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        stopRun(s.id);
      });
      menu.append(stopBtn);
    }

    const pinBtn = document.createElement('button');
    pinBtn.type = 'button';
    pinBtn.title = s.pinned ? t('session.unpin') : t('session.pin');
    pinBtn.innerHTML = PIN_SVG;
    if (s.pinned) pinBtn.classList.add('active');
    pinBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      togglePin(s);
    });

    const renameBtn = document.createElement('button');
    renameBtn.type = 'button';
    renameBtn.title = t('session.rename');
    renameBtn.innerHTML = icon('pencil', { size: 14 });
    renameBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      renameSession(s);
    });

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.title = t('session.delete');
    delBtn.innerHTML = icon('trash-2', { size: 14 });
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteSession(s.id);
    });

    menu.append(pinBtn, renameBtn, delBtn);
    item.append(main, pinMark, menu);
    sessionsList.appendChild(item);
  }

  // More chats exist than the sidebar page shows — open the full, paged list.
  if (_hasMore) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'sessions-more';
    more.textContent = t('nav.viewAll');
    more.addEventListener('click', () =>
      openAllSessionsDialog(sessionSearch?.value.trim() || ''),
    );
    sessionsList.appendChild(more);
  }

  // Sync header
  if (store.sessionId) {
    const active = store.sessions.find(s => s.id === store.sessionId);
    if (active?.title) {
      if (chatTitleEl) chatTitleEl.textContent = active.title;
    }
    if (exportWrap) exportWrap.style.display = '';
  } else {
    if (exportWrap) exportWrap.style.display = 'none';
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

  // Update header if this is the active session (fall back when cleared)
  if (sessionId === store.sessionId && chatTitleEl) {
    chatTitleEl.textContent = title || t('session.newChat');
  }
}

// ---- Actions -------------------------------------------------------------
async function renameSession(s) {
  const title = await promptDialog(t('dialog.renameChat'), s.title || '');
  if (title === null) return; // cancelled — empty string means "clear the title"
  try {
    await api.renameSession(s.id, title);
    updateSessionInSidebar(s.id, title);
  } catch (err) {
    console.error(err);
    toast(t('toast.renameFailed'), { type: 'error' });
  }
}

async function togglePin(s) {
  const next = !s.pinned;
  try {
    await api.setPinned(s.id, next);
  } catch (err) {
    console.error(err);
    toast(t('toast.pinFailed'), { type: 'error' });
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
  // Name what's about to disappear — a bare "this chat?" invites misclicks.
  // Untitled chats use the same display fallback the sidebar row shows.
  const target = store.sessions.find((s) => s.id === id);
  const ok = await confirmDialog(
    target
      ? t('dialog.deleteNamed', { title: target.title || t('session.newChat') })
      : t('dialog.deleteChat'),
  );
  if (!ok) return;
  try {
    await api.deleteSession(id);
  } catch (err) {
    console.error(err);
    toast(t('toast.deleteFailed'), { type: 'error' });
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
    // Align the switcher with the chat's own agent BEFORE the history replay:
    // the sync may reset the Files panel, which must not eat the replayed
    // workspace touches. Follow-ups then run on the agent this chat belongs to.
    store.emit('sync-agent', data.agent);
    if (chatTitleEl) chatTitleEl.textContent = data.title || t('session.newChat');
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
    h2.textContent = t('chat.couldntLoad');
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
  if (chatTitleEl) chatTitleEl.textContent = t('session.newChat');
  if (exportWrap) exportWrap.style.display = 'none';
  store.emit('reset-chat-view');
  renderSessions();
}

// ---- All chats dialog ------------------------------------------------------
// The full session list, loaded a page at a time ("Load more"), so a long
// history never lands in the sidebar DOM at once. Carries the sidebar's
// current filter and keeps paging it.
function openAllSessionsDialog(query = '') {
  const panel = document.createElement('div');
  panel.className = 'all-chats-panel';
  panel.innerHTML = `
    <div class="all-chats-head">
      <h3>${t('nav.allChats')}</h3>
      <button type="button" class="btn-icon all-chats-close" aria-label="Close">${icon('x', { size: 16 })}</button>
    </div>
    <div class="all-chats-list" role="list"></div>
    <button type="button" class="btn btn-ghost btn-sm all-chats-more" hidden>${t('nav.loadMore')}</button>`;
  const listEl = panel.querySelector('.all-chats-list');
  const moreBtn = panel.querySelector('.all-chats-more');
  let offset = 0;
  let loading = false;

  function rowFor(s) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'all-chats-item';
    if (s.id === store.sessionId) b.classList.add('active');
    b.title = s.title || s.id;
    const title = document.createElement('span');
    title.className = 'all-chats-title';
    title.textContent = s.title || t('session.newChat');
    const time = document.createElement('span');
    time.className = 'all-chats-time';
    time.textContent = formatTimeSmart(s.updated_at);
    time.title = formatDateTime(s.updated_at);
    b.append(title, time);
    b.addEventListener('click', () => {
      dialog.close();
      switchSession(s.id).catch(() => {});
    });
    return b;
  }

  async function loadPage() {
    if (loading) return;
    loading = true;
    moreBtn.disabled = true;
    try {
      // Same +1 sentinel as the sidebar. Offset paging can skip/repeat a row
      // if chats churn between pages — fine for a picker.
      const rows = await api.listSessions({ q: query, limit: PAGE_SIZE + 1, offset });
      const page = rows.slice(0, PAGE_SIZE);
      offset += page.length;
      listEl.append(...page.map(rowFor));
      moreBtn.hidden = rows.length <= PAGE_SIZE;
      if (!listEl.children.length) {
        const empty = document.createElement('div');
        empty.className = 'sessions-empty';
        empty.textContent = t('nav.none');
        listEl.appendChild(empty);
      }
    } catch (err) {
      console.error('openAllSessionsDialog:', err);
      toast(t('toast.loadChatsFailed'), { type: 'error' });
    } finally {
      loading = false;
      moreBtn.disabled = false;
    }
  }

  const dialog = showDialog({ body: panel });
  dialog.classList.add('dialog-wide');
  panel.querySelector('.all-chats-close').addEventListener('click', () => dialog.close());
  moreBtn.addEventListener('click', loadPage);
  loadPage();
}

// ---- Export --------------------------------------------------------------
export async function exportSession(format = 'md') {
  if (!store.sessionId) return;
  const title = store.sessions.find((s) => s.id === store.sessionId)?.title || '';
  if (format === 'html') return exportSessionHtml(store.sessionId, title);
  try {
    const res = await fetch(api.exportUrl(store.sessionId, format));
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = exportFilename(title, format);
    a.click();
    URL.revokeObjectURL(url);
    toast(t('toast.exported'));
  } catch (err) {
    console.error('export:', err);
    toast(t('toast.exportFailed'), { type: 'error' });
  }
}

// Dropdown letting the Export button pick a format (Markdown / HTML).
function initExportMenu() {
  const wrap = document.getElementById('export-wrap');
  const menu = document.getElementById('export-menu');
  if (!exportBtn || !menu || !wrap) return;
  const close = () => {
    menu.hidden = true;
    exportBtn.setAttribute('aria-expanded', 'false');
  };
  const open = () => {
    menu.hidden = false;
    exportBtn.setAttribute('aria-expanded', 'true');
  };
  exportBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.hidden ? open() : close();
  });
  menu.querySelectorAll('.export-menu-item').forEach((it) => {
    it.addEventListener('click', () => {
      close();
      exportSession(it.dataset.format);
    });
  });
  document.addEventListener('click', (e) => {
    if (!menu.hidden && !wrap.contains(e.target)) close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.hidden) close();
  });
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

// ---- Init ----------------------------------------------------------------
export function initSessions() {
  loadSessions();
  initSearch();

  document.getElementById('new-chat')?.addEventListener('click', () => {
    clearChat();
    document.getElementById('prompt')?.focus();
  });

  initExportMenu();
  store.on('clear-chat', clearChat);
  // Agents usually land after the first session render — the signature covers
  // the flip, so this redraws exactly once to add the agent chips.
  store.on('agents-loaded', renderSessions);

  // Keep the run dots honest and surface background completions.
  _schedulePoll();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      _unseenFinished = 0;
      document.title = _baseTitle; // clear the "(n)" badge
      loadSessions(sessionSearch?.value.trim() || '');
    }
    _schedulePoll(); // re-arm at the cadence matching the new visibility
  });
}
