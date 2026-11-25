// What runs behind the open chat: its subagent tasks and its background
// commands, in one list — the panel's Background tab — and one topbar button
// that says how they stand.
//
// The tasks come with the session list (sessions.js keeps `currentTasks` and
// the live runs); the commands are fetched here, when the server says they
// changed (`processes_changed`) and when the list comes into view. Nothing
// polls. The list's rules are background-items.js's.
import { t } from './i18n.js';
import { store } from './store.js';
import { api } from './api.js';
import { icon } from './icons.js';
import { toast } from './toast.js';
import { plainText } from './log-text.js';
import { elapsedNode, fmtElapsed } from './elapsed.js';
import { switchSession } from './sessions.js';
import { buildItems, buttonState, createFailureWatch } from './background-items.js';
import { outcomeText, processGuest } from './process-view.js';
import { openGuest, panelState, setPanel, viewedGuest } from './files.js';

const els = {};
let _procs = [];
let _procsOf = /** @type {string | null} */ (null); // the chat `_procs` is of
let _seq = 0; // the newest fetch; an older one's answer is stale
let _endedOpen = false;
let _armed = /** @type {string | null} */ (null); // the task whose stop asks again
let _armedTimer = null;
const _failures = createFailureWatch();

const items = () =>
  buildItems(
    _procsOf === store.sessionId ? _procs : [],
    (store.currentTasks || []).filter((s) => s.parent_id === store.sessionId),
    store.runsBySession,
  );
/** The tab is on. */
const inView = () => panelState().open && panelState().mode === 'background';
/** The list itself is before the user's eyes: not behind a log, a full-page
 * view or another browser tab. */
const onScreen = () =>
  inView() && !document.hidden && els.list.getClientRects().length > 0;

// ---- Commands ---------------------------------------------------------------

async function loadProcs() {
  const sid = store.sessionId;
  const seq = ++_seq;
  if (!sid) return void take(null, []);
  let procs;
  try {
    procs = await api.sessionProcesses(sid);
  } catch {
    return; // transient: the next event or the next look asks again
  }
  if (seq === _seq && store.sessionId === sid) take(sid, procs);
}

function take(sid, procs) {
  _procsOf = sid;
  _procs = procs;
  render();
  // Without file tabs, a panel with nothing to list is an empty box.
  const { live, ended } = items();
  if (inView() && !panelState().files && !live.length && !ended.length) setPanel(null);
  const guest = viewedGuest();
  const row = guest && procs.find((p) => `cmd:${p.process_id}` === guest.key);
  if (row) /** @type {any} */ (guest).update?.(row);
}

async function stopCommand(processId) {
  // If the user switches chats meanwhile, the answer is the old chat's list.
  const sid = store.sessionId;
  try {
    const procs = await api.killProcess(sid, processId);
    if (store.sessionId !== sid) return;
    _seq++; // fresher than any list on its way
    take(sid, procs);
    // It may have written files right up to its end.
    store.emit('workspace-maybe-stale');
  } catch (err) {
    toast(err?.message || t('bg.stopFailed'), { type: 'error' });
  }
}

/** Open a command's log in the panel. */
async function openProcess(id) {
  const sid = store.sessionId;
  if (!sid) return;
  const find = () => _procs.find((p) => p.process_id === id);
  if (_procsOf !== sid || !find()) await loadProcs();
  if (store.sessionId !== sid) return;
  const row = find();
  if (!row) {
    toast(t('bg.gone'));
    return;
  }
  setPanel('background');
  openGuest(processGuest(sid, row, { stop: stopCommand }));
}

// ---- Tasks ------------------------------------------------------------------

/** A task's stop asks once more: it ends where it is, and reports nothing. */
async function stopTask(item) {
  if (_armed !== item.key) {
    arm(item.key);
    return;
  }
  arm(null);
  try {
    await api.cancel(item.id);
  } catch (err) {
    toast(err?.message || t('bg.stopFailed'), { type: 'error' });
  }
}

function arm(key) {
  clearTimeout(_armedTimer);
  _armed = key;
  if (key) _armedTimer = setTimeout(() => arm(null), 4000);
  renderList();
}

// ---- The topbar button --------------------------------------------------------

function renderButton() {
  const { wrap, btn } = els;
  if (!wrap || !btn) return;
  const list = items();
  const total = list.live.length + list.ended.length;
  const parentId = store.currentParentId;
  wrap.style.display = total || parentId ? '' : 'none';
  if (parentId) {
    // Inside a task's chat the slot is the way home: plain navigation.
    btn.dataset.parent = parentId;
    btn.dataset.tone = '';
    btn.textContent = `← ${t('task.backToParent')}`;
    btn.removeAttribute('aria-expanded');
    btn.removeAttribute('aria-controls');
    return;
  }
  delete btn.dataset.parent;
  btn.setAttribute('aria-controls', 'files-panel');
  btn.setAttribute('aria-expanded', String(inView()));
  const { tone, running } = buttonState(list, _failures.unseen);
  btn.dataset.tone = tone;
  const dot = document.createElement('span');
  dot.className = 'lv-bg-dot';
  const label = document.createElement('span');
  label.textContent = running
    ? t('bg.running', { n: running })
    : t('bg.total', { n: total });
  const failed = tone === 'failed' ? t('bg.unseenFailure') : '';
  btn.title = failed;
  btn.replaceChildren(dot, label);
}

// ---- The list -----------------------------------------------------------------

let _lastSig = '';

function render() {
  const list = items();
  _failures.take(store.sessionId, list);
  if (onScreen()) _failures.seen();
  renderButton();
  renderList();
}

function renderList() {
  if (!els.list || !inView()) return;
  const list = items();
  const active = viewedGuest()?.key ?? null;
  // A rebuild would drop the hover and the keyboard's place for nothing.
  const sig = JSON.stringify([store.sessionId, list, _endedOpen, _armed, active]);
  if (sig === _lastSig) return;
  _lastSig = sig;

  const focused = els.list.contains(document.activeElement)
    ? /** @type {HTMLElement} */ (document.activeElement)
    : null;
  const at = focused && { key: focused.closest('[data-key]')?.getAttribute('data-key'), part: focused.dataset.part };

  const frag = document.createDocumentFragment();
  if (!list.live.length && !list.ended.length) {
    const none = document.createElement('div');
    none.className = 'lv-files-empty lv-files-empty-first';
    const title = document.createElement('div');
    title.className = 'lv-files-empty-title';
    title.textContent = t('bg.empty');
    const hint = document.createElement('div');
    hint.textContent = t('bg.emptyHint');
    none.append(title, hint);
    frag.append(none);
  }
  if (list.live.length) {
    const label = document.createElement('div');
    label.className = 'lv-files-group-label';
    label.textContent = `${t('bg.groupRunning')} ${list.live.length}`;
    frag.append(label, ...list.live.map((i) => row(i, active)));
  }
  if (list.ended.length) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'lv-files-group-label lv-files-group-toggle';
    toggle.dataset.part = 'ended';
    toggle.setAttribute('aria-expanded', String(_endedOpen));
    toggle.innerHTML = icon('chevron-right', { size: 12 });
    toggle.append(`${t('bg.groupEnded')} ${list.ended.length}`);
    toggle.addEventListener('click', () => {
      _endedOpen = !_endedOpen;
      renderList();
    });
    frag.append(toggle);
    if (_endedOpen) frag.append(...list.ended.map((i) => row(i, active)));
  }
  els.list.replaceChildren(frag);

  if (at?.part) {
    const scope = at.key
      ? els.list.querySelector(`[data-key="${CSS.escape(at.key)}"]`)
      : els.list;
    /** @type {HTMLElement | null | undefined} */ (
      scope?.querySelector(`[data-part="${at.part}"]`)
    )?.focus({ preventScroll: true });
  }
}

const TASK_OUTCOME = {
  completed: 'task.completed',
  failed: 'task.failed',
  stopped: 'task.cancelled',
  interrupted: 'task.interrupted',
};

/** The second line: what it is doing, or how it ended and how long it took. */
function detailText(item) {
  if (item.state === 'approval') return t('task.needsApproval');
  if (item.live) return item.kind === 'cmd' ? plainText(item.line) : t('task.running');
  const how =
    item.kind === 'task'
      ? t(TASK_OUTCOME[item.state] || 'task.completed')
      : outcomeText({ status: item.endedBy === 'exit' ? 'exited' : 'killed', exit_code: item.exitCode, ended_by: item.endedBy });
  const took =
    item.endedAt && item.startedAt ? ` · ${fmtElapsed(item.endedAt - item.startedAt)}` : '';
  return how + took;
}

/** @param {import('./background-items.js').BackgroundItem} item */
function row(item, active) {
  const el = document.createElement('div');
  el.className = `lv-bg-row lv-${item.state}`;
  el.classList.toggle('lv-active', item.key === active);
  el.dataset.key = item.key;

  const main = document.createElement('button');
  main.type = 'button';
  main.className = 'lv-bg-main';
  main.dataset.part = 'open';
  main.title = item.title;
  const glyph = document.createElement('span');
  glyph.className = 'lv-bg-icon';
  glyph.innerHTML = icon(item.kind === 'task' ? 'bot' : 'terminal', { size: 14 });
  const body = document.createElement('span');
  body.className = 'lv-bg-body';
  const head = document.createElement('span');
  head.className = `lv-bg-title lv-${item.kind}`;
  if (item.tag) {
    const tag = document.createElement('span');
    tag.className = 'lv-task-tag';
    tag.textContent = item.tag;
    head.append(tag);
  }
  const name = document.createElement('span');
  name.className = 'lv-bg-name';
  name.textContent = item.title;
  head.append(name);
  if (item.kind === 'task') {
    // It leaves the panel: the task's own chat opens.
    const out = document.createElement('span');
    out.className = 'lv-bg-out';
    out.innerHTML = icon('external-link', { size: 11 });
    head.append(out);
  }
  body.append(head);
  const detail = detailText(item);
  if (detail) {
    const line = document.createElement('span');
    line.className = 'lv-bg-line';
    line.textContent = detail;
    body.append(line);
  }
  main.append(glyph, body);
  main.addEventListener('click', () => {
    if (item.kind === 'task') void switchSession(item.id);
    else void openProcess(item.id);
  });
  el.append(main);

  if (item.live) {
    if (item.startedAt && item.state !== 'approval') {
      const clock = elapsedNode(item.startedAt);
      clock.classList.add('lv-bg-clock');
      el.append(clock);
    }
    const stop = document.createElement('button');
    stop.type = 'button';
    stop.dataset.part = 'stop';
    const armed = _armed === item.key;
    stop.className = armed ? 'lv-btn lv-btn-sm lv-btn-danger lv-bg-confirm' : 'lv-btn-icon lv-bg-stop';
    const label = armed ? t('bg.confirmStop') : t('bg.stop');
    if (armed) stop.textContent = label;
    else {
      stop.innerHTML = icon('square', { size: 13 });
      stop.title = label;
      stop.setAttribute('aria-label', label);
    }
    stop.addEventListener('click', async () => {
      if (item.kind === 'task') return void stopTask(item);
      stop.disabled = true;
      await stopCommand(item.id);
      stop.disabled = false;
    });
    el.append(stop);
  }
  return el;
}

// ---- Init -----------------------------------------------------------------------

/** Wire up the Background tab and its topbar button. */
export function initBackground() {
  els.wrap = document.getElementById('tasks-wrap');
  els.btn = document.getElementById('tasks-btn');
  els.list = document.getElementById('background-list');
  if (!els.btn || !els.list) return;

  els.btn.addEventListener('click', () => {
    const parent = els.btn.dataset.parent;
    if (parent) void switchSession(parent);
    else setPanel(inView() ? null : 'background');
  });

  store.on('tasks-changed', render);
  store.on('processes-changed', ({ session_id }) => {
    if (session_id === store.sessionId) void loadProcs();
  });
  // Every (re)connect of the event stream: what changed meanwhile was not told.
  store.on('events-open', () => void loadProcs());
  // Without the event stream (the fallback poll), the list says when to ask.
  store.on('sessions-loaded', () => {
    const row = store.sessions.find((s) => s.id === store.sessionId);
    const running = _procs.filter((p) => p.status === 'running').length;
    if (row && _procsOf === row.id && row.processes_running !== running) void loadProcs();
  });
  const chatChanged = () => {
    _armed = null;
    _endedOpen = false;
    render(); // what the chat just left had goes at once
    void loadProcs();
  };
  store.on('session-id', chatChanged);
  store.on('panel-changed', () => {
    const shown = inView();
    render();
    if (shown) void loadProcs(); // their last lines, as of now
  });
  store.on('open-process', ({ id }) => void openProcess(id));
  store.on('viewer-changed', render);
  document.addEventListener('visibilitychange', render);
  if (store.sessionId) void loadProcs();
}
