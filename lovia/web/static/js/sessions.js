// Session sidebar: list, search, switch, rename, delete, export.
import { t } from './i18n.js';
import { store } from './store.js';
import { api, apiError } from './api.js';
import { closeSidebar, promptDialog, confirmDialog, openMenu } from './ui.js';
import { toast } from './toast.js';
import { createRunNotices } from './run-notices.js';
import { icon } from './icons.js';
import { exportSessionHtml, exportFilename } from './export.js';
import { notificationsEnabled, playCompletionSound } from './settings.js';
import { dateBucket, formatDateTime, shortAge } from './util.js';
import { addWorkspaceDialog, canAddWorkspace, groupHeader, sidebarGroups, workspaceName } from './workspaces.js';
import { closeView, currentView, openView } from './views.js';
import { OPEN_SESSION_EVENT } from './builtin-views.js';

const sessionsList = document.getElementById('sessions-list');
const tasksWrap = document.getElementById('tasks-wrap');
const tasksBtn = document.getElementById('tasks-btn');
const tasksPopover = document.getElementById('tasks-popover');
const originBtn = /** @type {HTMLButtonElement | null} */ (
  document.getElementById('origin-btn')
);
const chatTitleEl = document.getElementById('chat-title');
const sessionSearch = /** @type {HTMLInputElement | null} */ (
  document.getElementById('session-search')
);
const chatMenuBtn = document.getElementById('chat-menu-btn');

// The sidebar holds a window of the chats and grows it on request — a bigger
// limit on the next fetch, never an offset, so a refresh redraws the same
// window. A flat list (no workspace groups, or a search) pages as a whole:
const FLAT_PAGE = 50;
let _flatLimit = FLAT_PAGE;
// Whether the last flat load had more past the limit (⇒ the "Load more" row).
let _hasMore = false;
// Workspace groups page each on their own: a group shows GROUP_PAGE chats
// until "Show more" adds GROUP_STEP (this map: group key → rows shown).
const GROUP_PAGE = 5;
const GROUP_STEP = 10;
/** @type {Map<string, number>} */
const _groupShown = new Map();
// The newest load wins: an older one answering late must not redraw.
let _loadSeq = 0;

// How the list is laid out where chats work in folders: a group per folder,
// or one list by date (the label's view menu; kept per browser).
const VIEW_KEY = 'lovia-sidebar-view';
/** @returns {'groups' | 'flat'} */
function sidebarView() {
  try {
    return localStorage.getItem(VIEW_KEY) === 'flat' ? 'flat' : 'groups';
  } catch {
    return 'groups';
  }
}
/** @param {'groups' | 'flat'} view */
function setSidebarView(view) {
  if (view === sidebarView()) return;
  try {
    localStorage.setItem(VIEW_KEY, view);
  } catch {
    return; // storage off: nowhere to keep the choice
  }
  _flatLimit = FLAT_PAGE;
  void loadSessions(); // the two views page differently
}

// The list shows the chats in use, or (the view menu) the archived ones — one
// list by date. Not kept: a reload is back on the chats in use.
let _showArchived = false;
/** @param {boolean} archived */
function setShowArchived(archived) {
  if (archived === _showArchived) return;
  _showArchived = archived;
  _flatLimit = FLAT_PAGE;
  void loadSessions();
}

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
// A disappeared run stays here until history confirms its outcome. Sidebar
// snapshots must stay current even when that separate lookup fails.
const _pendingRunResults = new Map(); // run ID → { sid, loading }
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
const runNotices = createRunNotices({
  notify: toast, t,
  titleOf: (sid) => store.sessions.find((s) => s.id === sid)?.title || t('toast.backgroundRun'),
});

function _markRunsSeen() {
  // A newer result must not move catch-up past an unresolved older one.
  if (_pendingRunResults.size) return;
  try {
    localStorage.setItem(RUNS_SEEN_KEY, String(Date.now() / 1000));
  } catch { /* storage unavailable (e.g. private mode) — feature degrades off */ }
}

function _readRunsSeen() {
  try {
    return parseFloat(localStorage.getItem(RUNS_SEEN_KEY));
  } catch {
    return NaN;
  }
}

async function checkMissedRuns(seen) {
  if (!Number.isFinite(seen)) {
    _markRunsSeen(); // first visit seeds the watermark quietly
    return;
  }
  let records;
  try {
    // Catch-up is one summary, regardless of how many records are returned.
    records = await api.runHistory({ since: seen, limit: 100 });
  } catch {
    // An older server or a blip: keep the old watermark so the next load
    // retries instead of silently dropping those notices forever.
    return;
  }
  _markRunsSeen();
  runNotices.add(records, { missed: true });
}

function _notifyRunFinished(sid, runId, status = 'completed') {
  // Whatever else happens below, this browser has now seen results up to here.
  _markRunsSeen();
  const run = { run_id: runId, session_id: sid, status };
  const stoppedAt = _recentlyStopped.get(sid);
  // A run this page streamed, in the chat still on screen, showed its own
  // outcome — no extra notice. Its stream usually ends before this event
  // arrives, so `streaming` alone misses it. A run the page never streamed
  // (another tab's, seen by the polling fallback) still gets one.
  const onScreen = sid === store.sessionId && !document.hidden && !currentView()
    && store.shownRuns.has(runId);
  if ((stoppedAt && Date.now() - stoppedAt < STOPPED_GRACE_MS)
    || (sid === store.sessionId && store.streaming) || onScreen) {
    runNotices.remember(run);
    return;
  }
  if (!runNotices.add([run])) return;
  const title = store.sessions.find((s) => s.id === sid)?.title || t('toast.backgroundRun');
  playCompletionSound(); // no-op unless the sound preference is on
  if (document.hidden) {
    _unseenFinished += 1;
    document.title = `(${_unseenFinished}) ${_baseTitle}`;
    // Only while hidden — a visible tab's toast is notification enough.
    if (notificationsEnabled()) {
      try {
        new Notification(_baseTitle, {
          body: t(status === 'failed' ? 'toast.runFailed' : 'toast.runFinished', { title }),
        });
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
  const es = new EventSource(api.eventsUrl());
  es.onopen = () => {
    _eventsLive = true;
    clearTimeout(_pollTimer);
    refresh();
    // Every (re)connect: state that only events keep current may have moved
    // while disconnected — listeners resync what the list snapshot can't.
    store.emit('events-open');
    // A run may have ended while disconnected, its run_finished missed.
    store.emit('runs-changed');
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
      if (d.status !== 'interrupted') _notifyRunFinished(d.session_id, d.run_id, d.status);
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
          src.startsWith('subagent-report:') ||
          src.startsWith('spawn:'))
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
  // A chat's permission mode was switched (this tab or another).
  es.addEventListener('session_workspace_changed', (e) => {
    try {
      store.emit('session-workspace-changed', JSON.parse(e.data));
    } catch { /* malformed payload — the next load resyncs */ }
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
    store.emit('session-meta', data);
    renderTasksButton();
    renderOrigin(data.origin);
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
  const seq = ++_loadSeq;
  // Where chats work in folders of their own, each folder's most recent
  // (and every pinned chat): one busy folder must not crowd the others out.
  const archived = _showArchived && !query; // a search covers both
  const grouped = !query && !archived && canAddWorkspace() && sidebarView() === 'groups';
  try {
    const [sessions, runs] = await Promise.all([
      // Fetch one row past the page: its presence answers "is there more?"
      // without a count endpoint or a response-shape change.
      api.listSessions(
        grouped
          ? { per_workspace: Math.max(GROUP_PAGE, ..._groupShown.values()) + 1, limit: 1000 }
          : { q: query, limit: _flatLimit + 1, archived },
      ),
      api.listRuns().catch(() => []),
    ]);
    if (seq !== _loadSeq) return;
    _hasMore = !grouped && sessions.length > _flatLimit;
    store.sessions = grouped ? sessions : sessions.slice(0, _flatLimit);
    const prevRuns = store.activeRuns || new Set();
    store.activeRuns = new Set(runs.map((r) => r.session_id));
    // Full live-run rows (status, started_at) — the task rows' badge data.
    const prevRows = store.runsBySession;
    store.runsBySession = new Map(runs.map((r) => [r.session_id, r]));
    // Diff-based completion detection belongs to the polling fallback; with
    // the event stream live, run_finished notifies directly (no double toast).
    if (_runsPrimed && !_eventsLive) {
      for (const sid of prevRuns) {
        if (!store.activeRuns.has(sid)) {
          // Absence from the active list does not tell success from failure.
          // Match the run we observed, not a newer run on the same chat.
          const runId = prevRows?.get(sid)?.run_id;
          if (runId && !_pendingRunResults.has(runId)) {
            _pendingRunResults.set(runId, { sid, loading: false });
          }
        }
      }
    }
    for (const [runId, pending] of _pendingRunResults) {
      if (pending.loading) continue;
      pending.loading = true;
      void api.runHistory({ run_id: runId, limit: 1 }).then((records) => {
        const record = records[0];
        if (!record || record.status === 'running') return; // finish may not be persisted yet
        _pendingRunResults.delete(runId);
        _notifyRunFinished(pending.sid, record.run_id, record.status);
      }).catch(() => { /* keep the ID for the next refresh */ })
        .finally(() => { pending.loading = false; });
    }
    _runsPrimed = true;
    _listLoaded = true;
    renderSessions();
    loadCurrentTasks();
    // Each snapshot (after a reconnect too) carries per-chat state that
    // events may have missed, such as a chat's permission mode.
    store.emit('sessions-loaded', store.sessions);
  } catch (err) {
    console.error('loadSessions:', err);
  }
}

// ---- Render --------------------------------------------------------------
// A cheap fingerprint of what renderSessions() draws, so repeated polls with
// identical data don't tear down and rebuild the whole sidebar.
let _lastRenderSig = null;
// Bumped when the workspace list or a group's folding changes: both shape the
// sidebar but live outside store.sessions.
let _workspacesRev = 0;
function sessionsSignature() {
  return JSON.stringify([
    new Date().toDateString(), // the date groups move at midnight
    store.sessionId,
    _hasMore,
    [..._groupShown],
    !!sessionSearch?.value.trim(), // the empty state's wording depends on it
    sidebarView(),
    _showArchived,
    store.agents.length > 1, // agent chips appear once agents finish loading
    [...(store.activeRuns || [])].sort(),
    store.sessions.map((s) => [
      s.id, s.title ?? '', s.updated_at, s.pinned ? 1 : 0, s.agent ?? '',
      s.parent_id ?? '', s.last_run_status ?? '', s.origin_kind ?? '',
      s.workspace_root ?? '', s.archived ? 1 : 0,
    ]),
    // The workspace list and which groups are folded (sidebarGroups reads both).
    _workspacesRev,
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
  if (_taskTicker != null || !document.querySelector('.lv-task-elapsed[data-started]')) return;
  _taskTicker = setInterval(() => {
    const els = document.querySelectorAll('.lv-task-elapsed[data-started]');
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
      tasksBtn.classList.remove('lv-attention');
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
  tasksBtn.classList.toggle('lv-attention', attention);
  tasksBtn.innerHTML = '';
  const dot = document.createElement('span');
  dot.className = `lv-task-dot ${attention ? 'lv-approval' : liveCount ? 'lv-running' : 'lv-completed'}`;
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
  row.className = 'lv-tasks-popover-row';
  row.setAttribute('role', 'menuitem');
  const d = document.createElement('span');
  d.className = `lv-task-dot lv-${state}`;
  const m = /^\[([^\]]{1,8})\]\s*(.*)$/.exec(s.title || '');
  const title = document.createElement('span');
  title.className = 'lv-tasks-popover-title';
  title.textContent = m ? m[2] || s.id : s.title || s.id;
  row.append(d);
  if (m) {
    const tag = document.createElement('span');
    tag.className = 'lv-task-tag';
    tag.textContent = m[1];
    row.append(tag);
  }
  row.append(title);
  const trail = document.createElement('span');
  trail.className = 'lv-task-elapsed';
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

// The date groups move at midnight even when no data does: re-render then
// (the poll loop is off while /api/events is live).
let _midnightTimer = null;
function scheduleMidnightRender() {
  clearTimeout(_midnightTimer);
  const now = new Date();
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  _midnightTimer = setTimeout(renderSessions, midnight.getTime() - now.getTime() + 1000);
}

function renderSessions() {
  if (!sessionsList) return;
  scheduleMidnightRender();
  const sig = sessionsSignature();
  if (sig === _lastRenderSig) return; // nothing changed — keep the DOM as-is
  _lastRenderSig = sig;
  sessionsList.innerHTML = '';

  // Task sessions (parent_id set) never render here — they belong to their
  // chat's topbar Tasks popover, not the global list.
  const chats = store.sessions.filter((s) => !s.parent_id);
  // Chats grouped by the directory they work in, where chats have one — a
  // search shows its matches flat (a chat whose directory left the list too),
  // as does the "one list" view.
  const searching = !!sessionSearch?.value.trim();
  const grouped = sidebarGroups(chats);
  syncSectionLabel(!!grouped);
  const groups = searching || _showArchived || sidebarView() === 'flat' ? null : grouped;
  if (groups) {
    renderGroups(groups);
    renderMore();
    syncHeader();
    return;
  }
  if (!chats.length) {
    const empty = document.createElement('div');
    empty.className = 'lv-sessions-empty';
    empty.textContent = t(
      searching ? 'nav.noMatches' : _showArchived ? 'nav.noArchived' : 'nav.noChats',
    );
    sessionsList.appendChild(empty);
    return;
  }
  // Pinned, then date groups (the server sorts pinned first, then by recency,
  // so each group is one run of rows). A lone date group needs no heading; the
  // Pinned one always has it — rows carry no pin mark of their own.
  const now = new Date();
  const groupOf = (s) => (s.pinned ? 'pinned' : dateBucket(s.updated_at, now));
  const headed = new Set(chats.map(groupOf)).size > 1 || chats.some((s) => s.pinned);
  let prevGroup = null;
  for (const s of chats) {
    const group = groupOf(s);
    if (headed && group !== prevGroup) {
      const head = document.createElement('div');
      head.className = 'lv-session-group';
      head.textContent = t(`nav.group.${group}`);
      sessionsList.appendChild(head);
    }
    prevGroup = group;
    // A search over chats in several folders: say which each is in.
    sessionsList.appendChild(buildRow(s, { folder: !!grouped }));
  }
  renderMore();
  syncHeader();
}

/**
 * One chat's sidebar row. `age` puts how long ago it was active at the end
 * (workspace groups have no date headings); `folder` names its folder
 * there instead, where rows of several folders mix (Pinned, a search).
 * @param {any} s
 * @param {{ age?: boolean, folder?: boolean }} [opts]
 */
function buildRow(s, { age = false, folder = false } = {}) {
    const item = document.createElement('div');
    item.className = 'lv-session-item';
    if (s.id === store.sessionId) item.classList.add('lv-active');
    if (store.activeRuns?.has(s.id)) item.classList.add('lv-running');
    if (s.pinned) item.classList.add('lv-pinned');
    item.dataset.id = s.id;

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'lv-session-main';
    main.title = sidebarTooltip(s);
    // A branch starts with its source's title, and a schedule's chat repeats
    // it run after run: mark both apart.
    if (s.origin_kind === 'fork' || s.origin_kind === 'schedule') {
      const mark = document.createElement('span');
      mark.className = 'lv-session-branch';
      mark.setAttribute('aria-hidden', 'true');
      mark.innerHTML = icon(s.origin_kind === 'fork' ? 'git-branch' : 'clock', { size: 12 });
      main.appendChild(mark);
    }
    const title = document.createElement('span');
    title.className = 'lv-session-title';
    title.textContent = s.title || t('session.newChat');
    main.appendChild(title);
    // Which brain a chat belongs to — only worth pixels when there's a choice.
    if (store.agents.length > 1 && s.agent) {
      const chip = document.createElement('span');
      chip.className = 'lv-session-agent';
      chip.textContent = s.agent;
      main.appendChild(chip);
    }
    if (age) {
      const end = document.createElement('span');
      end.className = 'lv-session-age';
      end.textContent = shortAge(s.updated_at);
      main.appendChild(end);
    } else if (s.archived && !_showArchived) {
      // A search reaches the archived chats too: say which those are.
      const end = document.createElement('span');
      end.className = 'lv-session-folder';
      end.textContent = t('nav.archivedTag');
      main.appendChild(end);
    } else if (folder && s.workspace_root) {
      const end = document.createElement('span');
      end.className = 'lv-session-folder';
      end.textContent = workspaceName(s.workspace_root);
      end.title = s.workspace_root;
      main.appendChild(end);
    }
    main.addEventListener('click', () => switchSession(s.id));

    item.append(main, rowActions(s, item));
    return item;
}

/**
 * The grouped sidebar: the pinned chats (all of them), then each
 * workspace's — its first GROUP_PAGE, more on request.
 * @param {import('./workspaces.js').SidebarGroup[]} groups
 */
function renderGroups(groups) {
  if (!sessionsList) return;
  const rerender = () => {
    _workspacesRev++;
    renderSessions();
  };
  for (const g of groups) {
    const section = document.createElement('section');
    section.className = 'lv-ws-group';
    section.dataset.kind = g.kind;
    const head = groupHeader(g, rerender);
    if (g.collapsed) {
      // Folded, its rows can't say it: the heading carries the open chat and
      // any live run inside.
      head.classList.toggle('lv-current', g.chats.some((s) => s.id === store.sessionId));
      head.classList.toggle('lv-running', g.chats.some((s) => store.activeRuns?.has(s.id)));
    }
    section.appendChild(head);
    if (!g.collapsed) {
      const rows = document.createElement('div');
      rows.className = 'lv-ws-group-rows';
      const pinned = g.kind === 'pinned';
      const shown = pinned ? Infinity : _groupShown.get(g.key) ?? GROUP_PAGE;
      for (const s of g.chats.slice(0, shown)) {
        rows.appendChild(buildRow(s, pinned ? { folder: true } : { age: true }));
      }
      if (!g.chats.length) {
        const empty = document.createElement('div');
        empty.className = 'lv-ws-group-empty';
        empty.textContent = t('ws.empty');
        rows.appendChild(empty);
      }
      const more = g.chats.length > shown;
      if (!pinned && (more || shown > GROUP_PAGE)) {
        rows.appendChild(groupPager(g.key, shown, more));
      }
      section.appendChild(rows);
    }
    sessionsList.appendChild(section);
  }
}

/**
 * The row under a group's chats: "Show more" while it has more, "Show less"
 * once it shows more than its first page.
 * @param {string} key @param {number} shown @param {boolean} more
 */
function groupPager(key, shown, more) {
  const row = document.createElement('div');
  row.className = 'lv-ws-group-pager';
  /** @param {string} label @param {() => void} onClick */
  const button = (label, onClick) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'lv-sessions-more';
    b.textContent = t(label);
    b.addEventListener('click', onClick);
    row.appendChild(b);
  };
  if (more) {
    button('nav.showMore', () => {
      _groupShown.set(key, shown + GROUP_STEP);
      void loadSessions(); // fetches the wider window, then redraws
    });
  }
  if (shown > GROUP_PAGE) {
    button('nav.showLess', () => {
      _groupShown.delete(key);
      renderSessions();
    });
  }
  return row;
}

/**
 * The list's label, "Chats": over groups it carries an add-a-folder button,
 * where more may be opened.
 * @param {boolean} grouped
 */
function syncSectionLabel(grouped) {
  const label = document.querySelector('[data-part="sessions-label"]');
  if (!label) return;
  let text = /** @type {HTMLElement | null} */ (label.querySelector('.lv-section-label-text'));
  if (!text) {
    text = document.createElement('span');
    text.className = 'lv-section-label-text';
    label.replaceChildren(text);
  }
  text.textContent = t(_showArchived ? 'nav.view.archived' : 'nav.chats');
  let tools = /** @type {HTMLElement | null} */ (label.querySelector('.lv-section-label-tools'));
  // Built once (a redraw must not pull an open menu's button from under it),
  // again only when what it offers changes.
  const canAdd = String(grouped && canAddWorkspace());
  if (tools?.dataset.add !== canAdd) {
    tools?.remove();
    tools = buildLabelTools(canAdd === 'true');
    tools.dataset.add = canAdd;
    label.appendChild(tools);
  }
  const view = /** @type {HTMLElement} */ (tools.querySelector('[aria-haspopup]'));
  view.dataset.grouped = String(grouped);
  view.classList.toggle('lv-active', _showArchived); // a filter is on
}

/**
 * The buttons at the label's end: the view menu, and add-a-folder where
 * more may be opened.
 * @param {boolean} canAdd
 */
function buildLabelTools(canAdd) {
  const tools = document.createElement('span');
  tools.className = 'lv-section-label-tools';
  /** @param {string} title @param {string} glyph */
  const button = (title, glyph) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'lv-btn-icon lv-section-label-btn';
    b.title = b.ariaLabel = t(title);
    b.innerHTML = icon(glyph, { size: 15 });
    tools.appendChild(b);
    return b;
  };
  const view = button('nav.view', 'sliders-horizontal');
  view.setAttribute('aria-haspopup', 'menu');
  view.setAttribute('aria-expanded', 'false');
  view.addEventListener('click', () =>
    openMenu(view, viewMenu(view.dataset.grouped === 'true')),
  );
  if (canAdd) {
    button('ws.add', 'folder-plus').addEventListener('click', () => void addWorkspaceDialog());
  }
  return tools;
}

/**
 * The list's view menu: where chats work in folders (`grouped`), a group per
 * folder or one list by date; and which chats — those in use, or the archived.
 * @param {boolean} grouped
 * @returns {(import('./ui.js').MenuItem | null)[]}
 */
function viewMenu(grouped) {
  const view = sidebarView();
  return [
    ...(grouped
      ? [
          { heading: t('nav.view.groupBy') },
          {
            label: t('nav.view.groups'),
            icon: icon('folder', { size: 15 }),
            checked: view === 'groups',
            onSelect: () => setSidebarView('groups'),
          },
          {
            label: t('nav.view.flat'),
            icon: icon('list', { size: 15 }),
            checked: view === 'flat',
            onSelect: () => setSidebarView('flat'),
          },
          null,
        ]
      : []),
    { heading: t('nav.view.show') },
    {
      label: t('nav.view.current'),
      icon: icon('square-pen', { size: 15 }),
      checked: !_showArchived,
      onSelect: () => setShowArchived(false),
    },
    {
      label: t('nav.view.archived'),
      icon: icon('archive', { size: 15 }),
      checked: _showArchived,
      onSelect: () => setShowArchived(true),
    },
  ];
}

function renderMore() {
  if (!sessionsList || !_hasMore) return;
  // A flat list (or a search) holding more than its window: widen it.
  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'lv-sessions-more';
  more.textContent = t('nav.loadMore');
  more.addEventListener('click', () => {
    _flatLimit += FLAT_PAGE;
    void loadSessions();
  });
  sessionsList.appendChild(more);
}

function syncHeader() {
  const active = store.sessions.find((s) => s.id === store.sessionId);
  if (active?.title && chatTitleEl) chatTitleEl.textContent = active.title;
}

/** A sidebar row's tooltip: its group says roughly when, this says exactly. */
function sidebarTooltip(s) {
  return `${s.title || s.id} · ${formatDateTime(s.updated_at)}`;
}

// ---- Update a single session's title wherever it shows ------------------
export function updateSessionTitle(sessionId, title) {
  // Update the cached sessions list
  const s = store.sessions.find(s => s.id === sessionId);
  if (s) s.title = title;
  if (_openMeta?.id === sessionId) _openMeta.title = title;

  const label = title || t('session.newChat');
  // Update the row directly, without a full re-render.
  const row = document.querySelector(`.lv-session-item[data-id="${sessionId}"]`);
  const titleEl = row?.querySelector('.lv-session-title');
  if (titleEl) titleEl.textContent = label;
  const main = /** @type {HTMLElement | null | undefined} */ (row?.querySelector('.lv-session-main'));
  if (main && s) main.title = sidebarTooltip(s);

  // Update header if this is the active session (fall back when cleared)
  if (sessionId === store.sessionId && chatTitleEl) chatTitleEl.textContent = label;
}

// ---- Actions -------------------------------------------------------------

/**
 * A chat's actions — one menu wherever it opens: a sidebar row's "⋯" or
 * right-click, and the topbar's "⋯" for the open chat. `s` is the chat as
 * the caller holds it: usually its `store.sessions` row, but the open chat
 * may be one the list doesn't carry — so each action updates `s` too.
 * @param {any} s
 * @returns {(import('./ui.js').MenuItem | null)[]}
 */
function chatActions(s) {
  const exportItem = (/** @type {string} */ format, /** @type {string} */ glyph) => ({
    label: t(`session.export.${format}`),
    icon: icon(glyph, { size: 14 }),
    onSelect: () => exportSession(format, s.id, s.title),
  });
  return [
    // A running chat can be stopped from the list — no need to open it just
    // to end its background run.
    ...(store.activeRuns?.has(s.id)
      ? [{
          label: t('session.stop'),
          icon: icon('square', { size: 14 }),
          onSelect: () => void stopRun(s.id),
        }]
      : []),
    // A task chat lives under the chat that started it, never in the list:
    // nothing to pin, and a branch of it would be an orphan.
    ...(s.parent_id
      ? []
      : [{
          label: t(s.pinned ? 'session.unpin' : 'session.pin'),
          icon: icon('pin', { size: 14 }),
          onSelect: () => void togglePin(s),
        }]),
    {
      label: t('session.rename'),
      icon: icon('pencil', { size: 14 }),
      onSelect: () => void renameSession(s),
    },
    ...(s.parent_id
      ? []
      : [{
          label: t('session.branch'),
          icon: icon('git-branch', { size: 14 }),
          onSelect: () => void forkChat(s.id),
        }]),
    // schedules.js answers it (where the server schedules) with its form.
    ...(s.parent_id || !store.canSchedule
      ? []
      : [{
          label: t('session.schedule'),
          icon: icon('clock', { size: 14 }),
          onSelect: () => store.emit('schedule-chat', { id: s.id, title: s.title, agent: s.agent }),
        }]),
    // Out of the list without deleting it (the view menu shows the archive).
    ...(s.parent_id
      ? []
      : [{
          label: t(s.archived ? 'session.unarchive' : 'session.archive'),
          icon: icon(s.archived ? 'archive-restore' : 'archive', { size: 14 }),
          onSelect: () => void toggleArchive(s),
        }]),
    null,
    exportItem('md', 'file-text'),
    exportItem('html', 'code'),
    exportItem('json', 'braces'),
    null,
    {
      label: t('session.delete'),
      icon: icon('trash-2', { size: 14 }),
      danger: true,
      onSelect: () => void deleteSession(s.id, s.title),
    },
  ];
}

/** What's known of the open chat when the list doesn't carry it (a task chat, one past the list's page). */
let _openMeta = /** @type {any} */ (null);

/** The open chat as the actions want it: its list row, else its detail. */
function openChat() {
  const id = store.sessionId;
  if (!id) return null;
  return (
    store.sessions.find((s) => s.id === id) ||
    (_openMeta?.id === id ? _openMeta : { id, title: '' })
  );
}

/**
 * The "⋯" button at a sidebar row's end: it opens the row's actions, as does
 * a right-click anywhere on the row. The items are built at open time, so
 * they follow the chat's current pinned / running state.
 */
function rowActions(s, /** @type {HTMLElement} */ row) {
  const items = () => chatActions(s);

  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'lv-session-more';
  more.title = t('session.actions');
  more.setAttribute('aria-label', t('session.actions'));
  more.setAttribute('aria-haspopup', 'menu');
  more.setAttribute('aria-expanded', 'false');
  more.innerHTML = icon('ellipsis', { size: 16 });
  more.addEventListener('click', (e) => {
    e.stopPropagation();
    openMenu(more, items());
  });
  row.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    openMenu({ x: e.clientX, y: e.clientY }, items());
  });
  return more;
}

/**
 * @typedef {{ kind: 'fork' | 'spawn' | 'schedule', session_id?: string | null,
 *   schedule_id?: string | null, title?: string | null, exists?: boolean }} Origin
 */

/** @type {Origin | null} */
let _origin = null;

/** Each origin kind's glyph and wording: [icon, label, label once the source is gone]. */
const ORIGIN_TEXT = {
  fork: ['git-branch', 'session.branchedFrom', 'session.branchSourceGone'],
  spawn: ['square-pen', 'session.startedFrom', 'session.startSourceGone'],
  schedule: ['clock', 'session.scheduledBy', 'session.scheduleGone'],
};

/**
 * The topbar link back to where this chat came from — the chat it branched
 * from or whose agent started it, or the schedule that made it — a session
 * detail's `origin`, or null to hide it. A deleted source stays named, inert.
 * @param {Origin | null | undefined} origin
 */
function renderOrigin(origin) {
  _origin = origin ?? null;
  if (!originBtn) return;
  originBtn.hidden = !origin;
  if (!origin) return;
  const [glyph, here, gone] = ORIGIN_TEXT[origin.kind] ?? ORIGIN_TEXT.fork;
  const label = document.createElement('span');
  const title = origin.title || t('session.newChat');
  label.textContent = t(origin.exists ? here : gone, { title });
  originBtn.title = label.textContent;
  originBtn.innerHTML = icon(glyph, { size: 14 });
  originBtn.append(label);
  originBtn.disabled = !origin.exists;
  originBtn.onclick = () => {
    if (origin.kind === 'schedule') openView(`lovia-schedules/${origin.schedule_id}`);
    else if (origin.session_id) void switchSession(origin.session_id);
  };
}

/**
 * Branch a chat into a new one and open it: whole, or up to just before the
 * user message `ref` names (see `POST /api/sessions/{id}/fork`). A user who
 * moved to another chat meanwhile stays there; the branch waits in the list.
 * @param {string} id
 * @param {string | null} [ref]
 * @returns {Promise<string | null>} The new chat's id, or null if it failed.
 */
export async function forkChat(id, ref = null) {
  const from = store.sessionId;
  let created;
  try {
    created = await api.forkSession(id, ref);
  } catch (err) {
    toast(err.message || t('chat.branchFailed'), { type: 'error' });
    return null;
  }
  await loadSessions();
  // Opened only if the user is still where they asked; else it waits in the list.
  if (store.sessionId === from) await switchSession(created.session_id);
  return created.session_id;
}

async function renameSession(s) {
  const title = await promptDialog(t('dialog.renameChat'), s.title || '');
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

async function togglePin(s) {
  const next = !s.pinned;
  try {
    await api.setPinned(s.id, next);
  } catch (err) {
    console.error(err);
    toast(t('toast.pinFailed'), { type: 'error' });
    return;
  }
  s.pinned = next;
  // A pin moves the chat between the Pinned section and its place in the
  // list, and shifts what the window holds (the server emits no event for
  // pins): refetch, under the active filter.
  await loadSessions();
}

async function toggleArchive(s) {
  const next = !s.archived;
  try {
    await api.setArchived(s.id, next);
  } catch (err) {
    console.error(err);
    toast(t('toast.archiveFailed'), { type: 'error' });
    return;
  }
  s.archived = next;
  if (next) s.pinned = false; // archiving unpins (the server did the same)
  // The open chat leaves the list with this: its menu reads what's kept here.
  if (_openMeta?.id === s.id) Object.assign(_openMeta, { archived: s.archived, pinned: s.pinned });
  toast(t(next ? 'toast.archived' : 'toast.unarchived', { title: s.title || t('session.newChat') }));
  await loadSessions();
}

/**
 * @param {string} [title] Name for the confirm; the topbar menu passes the
 *   open chat's own, which the list may not carry.
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
  // The user asked for a chat — the current one too: show it.
  closeView();
  closeSidebar();
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
  renderOrigin(null); // likewise
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
    if (data.exists === false && !data.active_run_id && !data.entries?.length) {
      // A chat the server doesn't have — a URL from another database or a
      // deleted chat: a new one, not one from before directories to link.
      startNewChat();
      return;
    }
    store.currentParentId = data.parent_id || null;
    store.emit('session-meta', data);
    renderTasksButton();
    renderOrigin(data.origin);
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
    // The chat is drawn (and reattached, if a run goes on): what waits for
    // its view — messages queued to send here — may act now.
    store.emit('session-loaded', id);
  } catch (err) {
    if (store.sessionId !== id) return; // superseded; don't clobber the new view
    const errState = document.createElement('div');
    errState.className = 'lv-empty-state';
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
  renderOrigin(null);
  if (chatTitleEl) chatTitleEl.textContent = t('session.newChat');
  store.emit('reset-chat-view');
  renderSessions();
}

/**
 * The user starts a new chat (button, shortcut, `lovia.newChat`). Unlike
 * clearChat — also run on an agent switch or deleting the current chat —
 * this shows the chat, closing any view.
 */
export function startNewChat() {
  closeView();
  closeSidebar();
  clearChat();
  // Even when the chat on screen is already a new one (no id change):
  // per-chat state such as the picked permission mode starts over.
  store.emit('new-chat');
  document.getElementById('prompt')?.focus();
}

// ---- Export --------------------------------------------------------------
/**
 * Download a chat as a file: Markdown and JSON from the server, HTML built
 * here (export.js).
 * @param {'md' | 'html' | 'json' | string} format
 * @param {string} id
 * @param {string} [title] Names the file.
 */
async function exportSession(format, id, title = '') {
  if (format === 'html') return exportSessionHtml(id, title);
  try {
    const res = await api.exportChat(id, format);
    if (!res.ok) throw await apiError(res);
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

// ---- Search --------------------------------------------------------------
let _searchTimer = null;
/** Wire up the sidebar chat filter (debounced input → reload; Enter = now). */
export function initSearch() {
  if (!sessionSearch) return;
  sessionSearch.addEventListener('input', () => {
    _flatLimit = FLAT_PAGE; // a new query starts from its first page
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
// The runs watermark as the page opened, for the catch-up after the first list.
let _bootSeen = NaN;
// Whether a list has landed: until then the sidebar draws nothing.
let _listLoaded = false;

/**
 * The first chat list, with the catch-up on runs that finished while no page
 * was open (after it, so their titles resolve). main.js calls it once the
 * workspace list has booted: drawn earlier, the list would show flat, then
 * regroup by folder.
 */
export async function loadFirstSessions() {
  await loadSessions();
  void checkMissedRuns(_bootSeen);
}

/** Boot the session sidebar: search, and background-run polling/notifications. */
export function initSessions() {
  // Captured now, before anything loads: a live completion must not advance
  // it past older, unseen results while the first list is in flight.
  _bootSeen = _readRunsSeen();
  initSearch();

  document.getElementById('new-chat')?.addEventListener('click', startNewChat);

  // The topbar's "⋯": the open chat's actions. A new chat has none yet.
  if (chatMenuBtn) {
    const sync = () => chatMenuBtn.classList.toggle('lv-hidden', !store.sessionId);
    store.on('session-id', sync);
    sync();
    chatMenuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const chat = openChat();
      if (chat) openMenu(chatMenuBtn, chatActions(chat));
    });
  }
  store.on('session-meta', (data) => {
    if (data?.id) {
      _openMeta = {
        id: data.id, title: data.title, pinned: data.pinned,
        archived: data.archived, parent_id: data.parent_id,
      };
    }
  });
  store.on('clear-chat', clearChat);
  // A workspace group's "+" (and the composer chip's menu): its directory is
  // already this tab's pick.
  store.on('new-chat-request', startNewChat);
  // The list decides how the sidebar pages (by folder, or as one), so a
  // change to it refetches.
  store.on('workspaces-changed', () => {
    _workspacesRev++;
    void loadSessions();
  });
  // Agents may land after the first list — the signature covers the flip, so
  // this redraws exactly once to add the agent chips. Before the first list
  // there is nothing to redraw ("No chats yet" would be a guess).
  store.on('agents-loaded', () => {
    if (_listLoaded) renderSessions();
  });
  // The open branch's source deleted from this tab: its link goes inert now,
  // not on the next load.
  store.on('session-deleted', (id) => {
    if (_origin?.session_id === id) renderOrigin({ ..._origin, exists: false });
  });
  // Likewise a schedule's chat, when its schedule goes.
  store.on('schedule-deleted', (id) => {
    if (_origin?.kind === 'schedule' && _origin.schedule_id === id) {
      renderOrigin({ ..._origin, exists: false });
    }
  });
  // A start_session card opens the chat it started.
  document.addEventListener(OPEN_SESSION_EVENT, (e) => {
    switchSession(/** @type {CustomEvent<string>} */ (e).detail);
  });

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
