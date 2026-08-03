// Session sidebar: list, search, switch, rename, delete, export.
import { t } from './i18n.js';
import { store } from './store.js';
import { api } from './api.js';
import { promptDialog, confirmDialog, showDialog } from './ui.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { exportSessionHtml, exportFilename } from './export.js';
import { notificationsEnabled, playCompletionSound } from './settings.js';
import { formatDateTime, formatTimeSmart } from './util.js';

const sessionsList = document.getElementById('sessions-list');
const tasksWrap = document.getElementById('tasks-wrap');
const tasksBtn = document.getElementById('tasks-btn');
const tasksPopover = document.getElementById('tasks-popover');
const chatTitleEl = document.getElementById('chat-title');
const sessionSearch = /** @type {HTMLInputElement | null} */ (
  document.getElementById('session-search')
);
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
// Preferred path: the server's /api/events lifecycle stream (initEventStream)
// pushes run/session changes; each (re)connect does one snapshot fetch and the
// poll loop below stays off. Fallback (feature off, or no EventSource):
// poll-and-diff — a session that was running and no longer is has finished —
// at a lively cadence while the tab is visible, slowly while hidden (a hidden
// tab is exactly where "your run finished" matters most).
const POLL_VISIBLE_MS = 8000;
const POLL_HIDDEN_MS = 30000;
const STOPPED_GRACE_MS = 10000;

let _pollTimer = null;
let _eventsLive = false; // /api/events connected → the poll loop stays off
let _runsPrimed = false; // first load only seeds the baseline — no notices
const _recentlyStopped = new Map(); // sid → ts of a UI-initiated stop
let _unseenFinished = 0; // completions while the tab was hidden
const _baseTitle = document.title;

// ---- Missed-completion catch-up ------------------------------------------
// While a page is open, completions arrive live (event stream or poll diff).
// This covers the rest: runs that finished while NO page was open. The
// localStorage watermark means "this browser knows all results up to T"; on
// load, anything in /api/runs/history that finished after it gets a catch-up
// toast, then the watermark moves to now.
const RUNS_SEEN_KEY = 'lovia-runs-seen';
const MISSED_TOAST_MAX = 5;

function _markRunsSeen() {
  try {
    localStorage.setItem(RUNS_SEEN_KEY, String(Date.now() / 1000));
  } catch { /* storage unavailable (e.g. private mode) — feature degrades off */ }
}

async function checkMissedRuns() {
  let seen = NaN;
  try {
    seen = parseFloat(localStorage.getItem(RUNS_SEEN_KEY));
  } catch {
    return;
  }
  if (!Number.isFinite(seen)) {
    _markRunsSeen(); // first visit seeds the watermark quietly
    return;
  }
  let records;
  try {
    // Generous limit: the toasts cap themselves below, but the watermark only
    // advances over what this fetch actually covered.
    records = await api.runHistory({ since: seen, limit: 100 });
  } catch {
    // An older server or a blip: keep the old watermark so the next load
    // retries instead of silently dropping those notices forever.
    return;
  }
  _markRunsSeen();
  // completed/failed are outcomes worth announcing; "cancelled" was the user's
  // own doing and "interrupted" is a resumable pause, not a result.
  const missed = records.filter(
    (r) => r.status === 'completed' || r.status === 'failed',
  );
  const titleOf = (sid) =>
    store.sessions.find((s) => s.id === sid)?.title || t('toast.backgroundRun');
  for (const r of missed.slice(0, MISSED_TOAST_MAX)) {
    const ok = r.status === 'completed';
    toast(
      t(ok ? 'toast.missedRunFinished' : 'toast.missedRunFailed', {
        title: titleOf(r.session_id),
      }),
      { type: ok ? 'success' : 'error' },
    );
  }
  if (missed.length > MISSED_TOAST_MAX) {
    toast(t('toast.missedRunMore', { n: missed.length - MISSED_TOAST_MAX }));
  }
}

function _notifyRunFinished(sid) {
  // Whatever else happens below, this browser has now seen results up to here.
  _markRunsSeen();
  const stoppedAt = _recentlyStopped.get(sid);
  if (stoppedAt && Date.now() - stoppedAt < STOPPED_GRACE_MS) return;
  // The chat on screen ends its own stream visibly — no extra notice.
  if (sid === store.sessionId && store.streaming) return;
  const title = store.sessions.find((s) => s.id === sid)?.title || t('toast.backgroundRun');
  toast(t('toast.runFinished', { title }), { type: 'success' });
  playCompletionSound(); // no-op unless the sound preference is on
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
  if (_eventsLive) return; // pushed, not polled
  _pollTimer = setTimeout(async () => {
    await loadSessions();
    _schedulePoll();
  }, document.hidden ? POLL_HIDDEN_MS : POLL_VISIBLE_MS);
}

// ---- /api/events lifecycle stream ----------------------------------------
/**
 * Subscribe to the server's /api/events lifecycle stream — one EventSource that
 * replaces the poll loop. No replay semantics: every open (first connect AND
 * each auto-reconnect) refetches one snapshot, then the stream keeps it current
 * — a disconnect gap is closed by the next snapshot.
 */
export function initEventStream() {
  if (typeof EventSource === 'undefined') return; // keep polling instead
  const refresh = () => loadSessions();
  const es = new EventSource('/api/events');
  es.onopen = () => {
    _eventsLive = true;
    clearTimeout(_pollTimer);
    refresh();
  };
  es.onerror = () => {
    // CONNECTING → a transient drop: EventSource retries by itself and the
    // next onopen's snapshot closes the gap; keep the poll off meanwhile.
    // CLOSED → the browser gave up for good (e.g. an auth failure) — fall
    // back to the poll loop so the sidebar doesn't silently freeze.
    if (es.readyState === EventSource.CLOSED) {
      _eventsLive = false;
      _schedulePoll();
    }
  };
  es.addEventListener('run_started', (e) => {
    try {
      const d = JSON.parse(e.data);
      // A background run began on the chat we're looking at (e.g. a schedule
      // fired into it) — attach to its live stream so it plays out on screen
      // instead of appearing only after a manual refresh.
      if (d.session_id === store.sessionId && !store.streaming) {
        store.emit('reconnect', d.session_id);
      }
    } catch { /* malformed payload — the sidebar refresh below still runs */ }
    store.emit('runs-changed');
    refresh();
  });
  es.addEventListener('run_finished', (e) => {
    try {
      const d = JSON.parse(e.data);
      // "interrupted" is a server shutdown/resumable pause, not an outcome.
      if (d.status !== 'interrupted') _notifyRunFinished(d.session_id);
      // A clientless run (a scheduled fire, or a subagent report delivery)
      // finished on the open chat before this tab attached to it (a fast run
      // wins the race against the reconnect above): reload the transcript so
      // its results appear. User-sourced runs never reload here — this
      // client just rendered its own stream.
      const src = String(d.source || '');
      if (
        d.session_id === store.sessionId &&
        !store.streaming &&
        (src.startsWith('schedule:') ||
          src.startsWith('subagent:') ||
          src.startsWith('subagent-report:'))
      ) {
        refreshTranscript();
      }
    } catch { /* malformed payload — the refresh below still fixes the UI */ }
    store.emit('runs-changed');
    refresh();
  });
  es.addEventListener('session_created', refresh);
  es.addEventListener('session_retitled', (e) => {
    try {
      const d = JSON.parse(e.data);
      updateSessionTitle(d.session_id, d.title);
    } catch { /* ignore */ }
  });
  // The model configuration changed (this tab or another): model-config.js
  // owns the reaction (chip label, config cache, agent refetch).
  es.addEventListener('config_changed', (e) => {
    try {
      store.emit('config-changed', JSON.parse(e.data));
    } catch { /* malformed payload — the next explicit load resyncs */ }
  });
}

/**
 * Re-fetch the open session's transcript and re-render it in place — used when
 * a background run landed new turns in the chat we're looking at.
 */
async function refreshTranscript() {
  const id = store.sessionId;
  if (!id || store.streaming) return;
  try {
    const data = await api.getSession(id);
    if (store.sessionId !== id || store.streaming) return; // superseded
    store.currentParentId = data.parent_id || null;
    renderTasksButton();
    if (chatTitleEl) chatTitleEl.textContent = data.title || t('session.newChat');
    store.emit('render-history', data.entries || []);
    if (data.active_run_id) store.emit('reconnect', id);
  } catch { /* transient — the next event or a manual switch repaints */ }
}

/** @returns {Promise<boolean>} True once the run was actually cancelled. */
async function stopRun(sid) {
  // Suppress the "finished" notice for a stop the user just asked for.
  _recentlyStopped.set(sid, Date.now());
  // Entries only matter within the grace window — don't let the map grow
  // for the lifetime of the tab.
  setTimeout(() => _recentlyStopped.delete(sid), STOPPED_GRACE_MS);
  let ok = true;
  try {
    await api.cancel(sid);
    toast(t('toast.runStopped'));
  } catch (err) {
    console.error('stopRun:', err);
    toast(t('toast.stopFailed'), { type: 'error' });
    ok = false;
  }
  loadSessions();
  return ok;
}

// ---- Load ----------------------------------------------------------------
/**
 * @param {string} [query] Filter substring; defaults to the live sidebar
 * filter so refreshes (delete, stream end, polls) keep an active search.
 */
export async function loadSessions(query = sessionSearch?.value.trim() ?? '') {
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
    // Full live-run rows (status, started_at) — the task rows' badge data.
    store.runsBySession = new Map(runs.map((r) => [r.session_id, r]));
    // Diff-based completion detection belongs to the polling fallback; with
    // the event stream live, run_finished notifies directly (no double toast).
    if (_runsPrimed && !_eventsLive) {
      for (const sid of prevRuns) {
        if (!store.activeRuns.has(sid)) _notifyRunFinished(sid);
      }
    }
    _runsPrimed = true;
    renderSessions();
    loadCurrentTasks();
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
    !!sessionSearch?.value.trim(), // the empty state's wording depends on it
    store.agents.length > 1, // agent chips appear once agents finish loading
    [...(store.activeRuns || [])].sort(),
    store.sessions.map((s) => [
      s.id, s.title ?? '', s.updated_at, s.pinned ? 1 : 0, s.agent ?? '',
      s.parent_id ?? '', s.last_run_status ?? '',
    ]),
    // Live-run status flips (running → blocked_on_approval) re-badge tasks.
    [...(store.runsBySession || new Map())].map(([id, r]) => `${id}:${r.status}`).sort(),
  ]);
}

// ---- Background tasks (this chat's subagent sessions) --------------------
// Task sessions never appear in the chat list; they belong to the chat that
// spawned them, surfaced by the topbar Tasks button + popover.

/** Lifecycle bucket for a task session: live status wins, else last outcome. */
function taskState(s) {
  const live = store.runsBySession?.get(s.id);
  if (live) return live.status === 'blocked_on_approval' ? 'approval' : 'running';
  return s.last_run_status || 'done';
}

function taskStatusLabel(state) {
  const key = {
    running: 'task.running',
    approval: 'task.needsApproval',
    completed: 'task.completed',
    done: 'task.completed', // no-record fallback — never leak the raw state
    failed: 'task.failed',
    cancelled: 'task.cancelled',
    interrupted: 'task.interrupted',
  }[state];
  return key ? t(key) : state;
}

function fmtElapsed(sec) {
  sec = Math.max(0, Math.floor(sec));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}

// One shared 1s ticker keeps running tasks' elapsed text fresh without
// re-rendering the sidebar; it stops itself when no task is running.
let _taskTicker = null;
function syncTaskTicker() {
  if (_taskTicker != null || !document.querySelector('.task-elapsed[data-started]')) return;
  _taskTicker = setInterval(() => {
    const els = document.querySelectorAll('.task-elapsed[data-started]');
    if (!els.length) {
      clearInterval(_taskTicker);
      _taskTicker = null;
      return;
    }
    const now = Date.now() / 1000;
    for (const el of els)
      el.textContent = fmtElapsed(now - Number(el.getAttribute('data-started')));
  }, 1000);
}

/** Fetch and render the open chat's tasks into the topbar button. */
export async function loadCurrentTasks() {
  if (!tasksWrap) return;
  const sid = store.sessionId;
  if (!sid) {
    store.currentTasks = [];
    renderTasksButton();
    return;
  }
  try {
    const tasks = await api.listSessions({ parent: sid });
    if (store.sessionId !== sid) return; // switched away mid-fetch
    store.currentTasks = tasks;
  } catch {
    store.currentTasks = [];
  }
  renderTasksButton();
}

function renderTasksButton() {
  if (!tasksWrap || !tasksBtn || !tasksPopover) return;
  const tasks = store.currentTasks || [];
  const parentId = store.currentParentId;
  tasksWrap.style.display = tasks.length || parentId ? '' : 'none';
  if (!tasks.length) {
    tasksPopover.hidden = true;
    tasksBtn.setAttribute('aria-expanded', 'false');
    if (parentId) {
      // Inside a task session: the same topbar slot becomes the way home —
      // plain navigation, so drop the menu semantics along with the popover.
      tasksBtn.dataset.parent = parentId;
      tasksBtn.classList.remove('attention');
      tasksBtn.textContent = `← ${t('task.backToParent')}`;
      tasksBtn.removeAttribute('aria-haspopup');
      tasksBtn.removeAttribute('aria-controls');
      tasksBtn.removeAttribute('aria-expanded');
    }
    return;
  }
  delete tasksBtn.dataset.parent;
  tasksBtn.setAttribute('aria-haspopup', 'menu');
  tasksBtn.setAttribute('aria-controls', 'tasks-popover');
  if (!tasksBtn.hasAttribute('aria-expanded')) {
    tasksBtn.setAttribute('aria-expanded', 'false');
  }
  const liveCount = tasks.filter((s) => store.runsBySession?.has(s.id)).length;
  const attention = tasks.some((s) => taskState(s) === 'approval');
  tasksBtn.classList.toggle('attention', attention);
  tasksBtn.innerHTML = '';
  const dot = document.createElement('span');
  dot.className = `task-dot ${attention ? 'approval' : liveCount ? 'running' : 'completed'}`;
  const label = document.createElement('span');
  label.textContent = liveCount
    ? t('nav.tasksRunning', { n: liveCount })
    : `${t('nav.tasks')} ${tasks.length}`;
  tasksBtn.append(dot, label);
  tasksPopover.innerHTML = '';
  for (const s of tasks) {
    tasksPopover.appendChild(buildTaskRow(s));
  }
  syncTaskTicker();
}

/** One popover row: [tN] chip + title + status dot + elapsed/outcome. */
function buildTaskRow(s) {
  const state = taskState(s);
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'tasks-popover-row';
  row.setAttribute('role', 'menuitem');
  const d = document.createElement('span');
  d.className = `task-dot ${state}`;
  const m = /^\[([^\]]{1,8})\]\s*(.*)$/.exec(s.title || '');
  const title = document.createElement('span');
  title.className = 'tasks-popover-title';
  title.textContent = m ? m[2] || s.id : s.title || s.id;
  row.append(d);
  if (m) {
    const tag = document.createElement('span');
    tag.className = 'task-tag';
    tag.textContent = m[1];
    row.append(tag);
  }
  row.append(title);
  const trail = document.createElement('span');
  trail.className = 'task-elapsed';
  const live = store.runsBySession?.get(s.id);
  if (live?.started_at) {
    trail.dataset.started = String(live.started_at);
    trail.textContent = fmtElapsed(Date.now() / 1000 - live.started_at);
  } else {
    trail.textContent = taskStatusLabel(state);
  }
  row.append(trail);
  row.addEventListener('click', () => {
    tasksPopover.hidden = true;
    tasksBtn?.setAttribute('aria-expanded', 'false');
    switchSession(s.id);
  });
  return row;
}

if (tasksBtn && tasksPopover) {
  tasksBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (tasksBtn.dataset.parent) {
      switchSession(tasksBtn.dataset.parent);
      return;
    }
    tasksPopover.hidden = !tasksPopover.hidden;
    tasksBtn.setAttribute('aria-expanded', String(!tasksPopover.hidden));
  });
  document.addEventListener('click', (e) => {
    const target = e.target instanceof Node ? e.target : null;
    if (!tasksPopover.hidden && !tasksPopover.contains(target)) {
      tasksPopover.hidden = true;
      tasksBtn.setAttribute('aria-expanded', 'false');
    }
  });
}

function renderSessions() {
  if (!sessionsList) return;
  const sig = sessionsSignature();
  if (sig === _lastRenderSig) return; // nothing changed — keep the DOM as-is
  _lastRenderSig = sig;
  sessionsList.innerHTML = '';

  // Task sessions (parent_id set) never render here — they belong to their
  // chat's topbar Tasks popover, not the global list.
  const chats = store.sessions.filter((s) => !s.parent_id);
  if (!chats.length) {
    const empty = document.createElement('div');
    empty.className = 'sessions-empty';
    empty.textContent = t(sessionSearch?.value.trim() ? 'nav.noMatches' : 'nav.noChats');
    sessionsList.appendChild(empty);
    return;
  }
  let prevPinned = false;
  for (const s of chats) {
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
    const meta = /** @type {HTMLElement} */ (main.querySelector('.session-meta'));
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

    item.append(main, ...rowActions(s, item));
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

// ---- Update a single session's title wherever it shows ------------------
export function updateSessionTitle(sessionId, title) {
  // Update the cached sessions list
  const s = store.sessions.find(s => s.id === sessionId);
  if (s) s.title = title;

  const label = title || t('session.newChat');
  // Update the DOM directly without full re-render. Both lists key rows by
  // session id and can be on screen at once (the all-chats dialog sits over the
  // sidebar), so a rename from either has to repaint both.
  const rows = `.session-item[data-id="${sessionId}"], .all-chats-item[data-id="${sessionId}"]`;
  document.querySelectorAll(rows).forEach((row) => {
    const titleEl = row.querySelector('.session-title, .all-chats-title');
    if (titleEl) titleEl.textContent = label;
    const main = /** @type {HTMLElement | null} */ (row.querySelector('.session-main, .all-chats-main'));
    if (main) main.title = title || sessionId; // same fallback the rows render with
  });

  // Update header if this is the active session (fall back when cleared)
  if (sessionId === store.sessionId && chatTitleEl) chatTitleEl.textContent = label;
}

// ---- Actions -------------------------------------------------------------
// Both chat lists share these. `s` is whatever row object the caller holds —
// the all-chats dialog pages straight from the API, so its rows are NOT the
// cached `store.sessions` objects. Each action therefore updates the row it was
// handed as well as the cache, and reports back so the caller can repaint.

/**
 * The at-rest pin marker + hover action menu both chat lists append to a row.
 *
 * Handlers restate `row` themselves instead of leaning on a re-render: the
 * all-chats dialog's rows are standalone and nothing repaints them. In the
 * sidebar the same lines land on a row `renderSessions` already replaced — a
 * no-op on a detached node.
 *
 * `stacked` layers the rename prompt over an open dialog instead of closing it;
 * `onDeleted` runs once a delete lands. Both options are typed by inference off
 * the defaults below — a `@param` tag naming a destructured parameter fails
 * `tsc --checkJs` (TS8024), and one without a default drops out of the type.
 */
function rowActions(s, /** @type {HTMLElement} */ row, { onDeleted = () => {}, stacked = false } = {}) {
  const pinMark = document.createElement('span');
  pinMark.className = 'session-pin';
  pinMark.setAttribute('aria-hidden', 'true');
  pinMark.innerHTML = PIN_SVG;

  const menu = document.createElement('div');
  menu.className = 'session-menu';
  const add = (title, svg, onClick) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.title = title;
    b.innerHTML = svg;
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      onClick(b);
    });
    menu.append(b);
    return b;
  };

  // A running chat gets a stop control right in the list — no need to open it
  // just to end its background run.
  if (store.activeRuns?.has(s.id)) {
    add(t('session.stop'), icon('square', { size: 13 }), async (b) => {
      b.disabled = true;
      if (!(await stopRun(s.id))) {
        b.disabled = false; // still running — leave the control usable
        return;
      }
      b.remove();
      row.classList.remove('running');
    }).classList.add('session-stop');
  }

  const pinBtn = add(
    s.pinned ? t('session.unpin') : t('session.pin'),
    PIN_SVG,
    async () => {
      const pinned = await togglePin(s);
      if (pinned === null) return;
      // Restate in place — neither list re-sorts a row out from under the pointer.
      row.classList.toggle('pinned', pinned);
      pinBtn.classList.toggle('active', pinned);
      pinBtn.title = pinned ? t('session.unpin') : t('session.pin');
    },
  );
  pinBtn.classList.toggle('active', !!s.pinned);

  add(t('session.rename'), icon('pencil', { size: 14 }), () =>
    renameSession(s, { stack: stacked }),
  );
  add(t('session.delete'), icon('trash-2', { size: 14 }), async () => {
    if (await deleteSession(s.id, s.title)) onDeleted();
  });

  return [pinMark, menu];
}

/** `stack` keeps an already-open dialog alive under the prompt. */
async function renameSession(s, { stack = false } = {}) {
  const title = await promptDialog(t('dialog.renameChat'), s.title || '', { stack });
  if (title === null) return; // cancelled — empty string means "clear the title"
  try {
    await api.renameSession(s.id, title);
  } catch (err) {
    console.error(err);
    toast(t('toast.renameFailed'), { type: 'error' });
    return;
  }
  s.title = title;
  updateSessionTitle(s.id, title);
}

/** @returns {Promise<boolean | null>} The new pinned state, or null if it failed. */
async function togglePin(s) {
  const next = !s.pinned;
  try {
    await api.setPinned(s.id, next);
  } catch (err) {
    console.error(err);
    toast(t('toast.pinFailed'), { type: 'error' });
    return null;
  }
  s.pinned = next;
  const target = store.sessions.find((x) => x.id === s.id);
  if (!target) {
    // An all-chats row from beyond the sidebar's page: pinning promotes it into
    // that page, and the server emits no event for pins — so refetch, or the
    // sidebar would keep the stale order until some unrelated poll.
    await loadSessions();
    return next;
  }
  // Update locally and re-sort to match the server's "pinned first, then most
  // recent" order — cheaper than a reload, and it keeps the active search filter.
  target.pinned = next;
  store.sessions.sort(
    (a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || b.updated_at - a.updated_at,
  );
  _lastRenderSig = null; // order changed — force a redraw
  renderSessions();
  return next;
}

/**
 * @param {string} [title] Name for the confirm; all-chats rows pass their own
 *   because they can be past the sidebar's page, where the lookup finds nothing.
 * @returns {Promise<boolean>} True once the chat is gone.
 */
export async function deleteSession(
  id,
  title = store.sessions.find((s) => s.id === id)?.title,
) {
  // Name what's about to disappear — a bare "this chat?" invites misclicks.
  // Untitled chats use the same display fallback the list rows show.
  const ok = await confirmDialog(
    title === undefined
      ? t('dialog.deleteChat')
      : t('dialog.deleteNamed', { title: title || t('session.newChat') }),
  );
  if (!ok) return false;
  try {
    await api.deleteSession(id);
  } catch (err) {
    console.error(err);
    toast(t('toast.deleteFailed'), { type: 'error' });
    return false; // leave the view untouched if the delete didn't land
  }
  store.emit('session-deleted', id); // e.g. drop the chat's saved draft
  if (store.sessionId === id) {
    store.sessionId = null;
    store.emit('clear-chat');
  }
  await loadSessions();
  return true;
}

/** @param {string} id */
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
  store.currentParentId = null; // re-learned from the session detail below
  loadCurrentTasks();

  const transcript = document.getElementById('transcript');
  const emptyState = document.getElementById('empty-state');
  if (emptyState) emptyState.remove();

  transcript.replaceChildren(
    /** @type {HTMLTemplateElement} */ (
      document.getElementById('tmpl-skeleton')
    ).content.cloneNode(true),
  );
  renderSessions();

  try {
    const data = await api.getSession(id);
    if (store.sessionId !== id) return; // a newer switch superseded this one
    store.currentParentId = data.parent_id || null;
    renderTasksButton();
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
    } else {
      // Settled history — repaint cached follow-up chips if they still fit the
      // tail. Skipped while a run is live: its own completion produces fresh ones.
      store.emit('maybe-followups', data.entries || []);
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

/** Clear the active session and show the empty new-chat state. */
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
      <button type="button" class="btn-icon all-chats-close" aria-label="${t('dialog.close')}">${icon('x', { size: 16 })}</button>
    </div>
    <div class="all-chats-list" role="list"></div>
    <button type="button" class="btn btn-ghost btn-sm all-chats-more" hidden>${t('nav.loadMore')}</button>`;
  const listEl = panel.querySelector('.all-chats-list');
  const moreBtn = /** @type {HTMLButtonElement} */ (panel.querySelector('.all-chats-more'));
  let offset = 0;
  let loading = false;

  // Same three-slot row as the sidebar (main button, pin marker, action menu) —
  // only the main button's layout differs, so the actions come from rowActions.
  function rowFor(s) {
    const row = document.createElement('div');
    row.className = 'all-chats-item';
    row.dataset.id = s.id;
    if (s.id === store.sessionId) row.classList.add('active');
    if (store.activeRuns?.has(s.id)) row.classList.add('running');
    if (s.pinned) row.classList.add('pinned');

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'all-chats-main';
    main.title = s.title || s.id;
    const title = document.createElement('span');
    title.className = 'all-chats-title';
    title.textContent = s.title || t('session.newChat');
    const time = document.createElement('span');
    time.className = 'all-chats-time';
    time.textContent = formatTimeSmart(s.updated_at);
    time.title = formatDateTime(s.updated_at);
    main.append(title, time);
    main.addEventListener('click', () => {
      dialog.close();
      switchSession(s.id).catch(() => {});
    });

    row.append(main, ...rowActions(s, row, {
      stacked: true, // a prompt must not close the list it was opened from
      onDeleted: () => {
        row.remove();
        offset = Math.max(0, offset - 1); // or the next page skips a chat
        syncEmpty();
      },
    }));
    return row;
  }

  /** Show the empty notice only while the list really has no rows. */
  function syncEmpty() {
    const empty = listEl.querySelector('.sessions-empty');
    if (listEl.querySelector('.all-chats-item')) {
      empty?.remove();
    } else if (!empty) {
      const el = document.createElement('div');
      el.className = 'sessions-empty';
      el.textContent = t('nav.none');
      listEl.appendChild(el);
    }
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
      syncEmpty();
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
/** @param {'md' | 'html' | string} [format] */
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
  menu.querySelectorAll('.export-menu-item').forEach((/** @type {HTMLElement} */ it) => {
    it.addEventListener('click', () => {
      close();
      exportSession(it.dataset.format);
    });
  });
  document.addEventListener('click', (e) => {
    if (!menu.hidden && !wrap.contains(/** @type {Node} */ (e.target))) close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.hidden) close();
  });
}

// ---- Search --------------------------------------------------------------
let _searchTimer = null;
/** Wire up the sidebar chat filter (debounced input → reload; Enter = now). */
export function initSearch() {
  if (!sessionSearch) return;
  sessionSearch.addEventListener('input', () => {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => loadSessions(), 250);
  });
  sessionSearch.addEventListener('keydown', (e) => {
    // isComposing: Enter that confirms an IME candidate isn't a search.
    if (e.key !== 'Enter' || e.isComposing) return;
    clearTimeout(_searchTimer);
    loadSessions();
  });
}

// ---- Init ----------------------------------------------------------------
/** Boot the session sidebar: initial load, search, and background-run polling/notifications. */
export function initSessions() {
  // Catch-up runs after the first session load so titles are resolvable.
  loadSessions().then(checkMissedRuns);
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
      loadSessions();
    }
    _schedulePoll(); // re-arm at the cadence matching the new visibility
  });
}
