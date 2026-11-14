// schedules.js — the Scheduled runs page, lovia's own view (views.js):
// `?view=lovia-schedules` lists the schedules, `?view=lovia-schedules/<id>`
// shows one with its history. Creating and editing happen in a dialog over
// the page. Design: docs/design/web-schedules.md.
//
// The page re-reads on the `runs-changed` store event (relayed from the
// /api/events stream by sessions.js), so a firing schedule shows a live
// "running" state that settles into its outcome by itself.
import { t } from './i18n.js';
import { api } from './api.js';
import { store } from './store.js';
import { showDialog, confirmDialog, openMenu } from './ui.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { formatDateTime } from './util.js';
import { switchSession } from './sessions.js';
import { collectContext } from './lovia.js';
import { openView, registerBuiltinView } from './views.js';
import {
  addWorkspaceDialog,
  canAddWorkspace,
  scopeRoot,
  workspaceChoices,
  workspaceName,
  workspacesOn,
} from './workspaces.js';
import { modeChoices, modeGlyph, modeLabel } from './workspace-mode.js';
import {
  WEEK,
  dailyCron,
  describeTrigger,
  displayName,
  finishedReason,
  formatWhen,
  humanizeCron,
  isDone,
  parseDaily,
  splitInterval,
  statusOf,
  unitSeconds,
} from './schedule-time.js';

const VIEW = 'lovia-schedules';
const HISTORY_PAGE = 20;

/**
 * @typedef {import('./schedule-time.js').Schedule & {
 *   agent?: string | null, session_id?: string | null, session_title?: string | null,
 *   workspace_root?: string | null, workspace_mode?: string | null,
 *   last_status?: string | null, last_error?: string | null,
 *   until?: string | null, max_fires?: number | null, expires_at?: number | null,
 *   fire_count?: number, created_at: number }} Row
 * @typedef {{ rows: Row[], firing: Map<string, string> }} Data `firing`: the
 *   schedules with a live fired run → the chat it runs in.
 */

/**
 * @param {string} tag
 * @param {string} [cls]
 * @param {string} [text]
 */
function el(tag, cls = '', text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/**
 * @param {string} label
 * @param {string | null} glyph
 * @param {() => void} onClick
 * @param {string} [cls]
 */
function button(label, glyph, onClick, cls = 'lv-btn lv-btn-ghost lv-btn-sm') {
  const b = /** @type {HTMLButtonElement} */ (el('button', cls));
  b.type = 'button';
  if (glyph) b.innerHTML = icon(glyph, { size: 14 });
  b.append(el('span', '', label));
  b.addEventListener('click', onClick);
  return b;
}

/** @param {boolean} on @param {string} label @param {(on: boolean) => void} onChange */
function toggle(on, label, onChange) {
  const sw = el('button', 'lv-switch');
  sw.setAttribute('type', 'button');
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', String(on));
  sw.setAttribute('aria-label', label);
  sw.title = label;
  sw.append(el('span', 'lv-switch-knob'));
  sw.addEventListener('click', (e) => {
    e.stopPropagation();
    onChange(sw.getAttribute('aria-checked') !== 'true');
  });
  return sw;
}

/** @returns {Promise<Data>} */
async function load() {
  const [rows, runs] = await Promise.all([
    api.listSchedules(),
    api.listRuns().catch(() => []),
  ]);
  const firing = new Map();
  for (const r of runs) {
    const src = String(r.source || '');
    if (src.startsWith('schedule:')) firing.set(src.slice('schedule:'.length), r.session_id);
  }
  return { rows, firing };
}

/** @param {string} sid */
function openChat(sid) {
  switchSession(sid).catch(() => {});
}

// ---- actions shared by the list and a schedule's page ------------------------

/** @param {Row} s @param {boolean} on @param {() => void} done */
async function setActive(s, on, done) {
  try {
    await api.setScheduleActive(s.id, on);
  } catch (err) {
    toast(err.message || t('sched.updateFailed'), { type: 'error' });
  }
  done();
}

/** @param {Row} s @param {() => void} done */
async function runNow(s, done) {
  try {
    await api.runSchedule(s.id);
    toast(t('sched.fired'));
  } catch (err) {
    toast(err.message || t('sched.runFailed'), { type: 'error' });
  }
  done();
}

/** @param {Row} s */
async function remove(s) {
  if (!(await confirmDialog(t('sched.deleteConfirm', { name: displayName(s) })))) return;
  try {
    await api.deleteSchedule(s.id);
  } catch (err) {
    // A 404 means it is gone already: the list will agree.
    if (err.status !== 404) toast(err.message || t('sched.deleteFailed'), { type: 'error' });
  }
  store.emit('schedules-changed'); // the top bar's list of the chat's schedules
  openView(VIEW);
}

/** @param {Row} s */
function duplicate(s) {
  const { id: _id, ...rest } = s;
  openScheduleForm({ preset: { ...rest, name: t('sched.copyName', { name: displayName(s) }) } });
}

/**
 * The ⋯ menu: what a row or a schedule's page doesn't show as a button.
 * @param {HTMLElement} at @param {Row} s @param {() => void} done
 * @param {{ run?: boolean }} [opts]
 */
function moreMenu(at, s, done, { run = true } = {}) {
  /** @type {(import('./ui.js').MenuItem | null)[]} */
  const items = [];
  if (run) {
    items.push({ label: t('sched.runNow'), icon: icon('play', { size: 15 }), onSelect: () => runNow(s, done) });
  }
  items.push(
    { label: t('sched.edit'), icon: icon('pencil', { size: 15 }), onSelect: () => openScheduleForm({ editing: s, onSaved: done }) },
    { label: t('sched.duplicate'), icon: icon('copy', { size: 15 }), onSelect: () => duplicate(s) },
    null,
    { label: t('sched.delete'), icon: icon('trash-2', { size: 15 }), danger: true, onSelect: () => remove(s) },
  );
  openMenu(at, items);
}

/** @param {string} st */
function statusLabel(st) {
  return {
    running: t('sched.stRunning'),
    active: t('sched.stActive'),
    paused: t('sched.stPaused'),
    done: t('sched.stDone'),
  }[st];
}

/** @param {string} st */
function dot(st) {
  const d = el('span', `lv-sched-dot lv-${st}`);
  d.title = statusLabel(st) ?? '';
  return d;
}

/**
 * The quiet facts under a schedule's name: where it works, where results go,
 * when it stops. Only what was set.
 * @param {Row} s
 */
function extraFacts(s) {
  const parts = [];
  if (s.workspace_root && workspacesOn()) parts.push(workspaceName(s.workspace_root));
  if (s.session_id) {
    parts.push(t('sched.continuesShort', { title: s.session_title || t('session.newChat') }));
  }
  if (s.until) parts.push(t('sched.untilShort', { cond: s.until }));
  if (s.max_fires != null) parts.push(t('sched.firesOf', { n: s.fire_count ?? 0, m: s.max_fires }));
  return parts;
}

/**
 * Where a schedule stands, in one phrase: when it runs next, that it is
 * running, paused, or why it ended. Null for a pending one-shot, whose
 * trigger already says when.
 * @param {Row} s @param {string} st
 * @returns {string | null}
 */
function standing(s, st) {
  if (st === 'running') return t('sched.runningNow');
  if (st === 'active' && s.trigger_kind === 'at') return null;
  if (st === 'active') return t('sched.next', { time: formatWhen(s.next_fire) });
  if (st === 'done') {
    return s.finished_reason
      ? t('sched.endedWhy', { why: finishedReason(s.finished_reason) })
      : t('sched.stDone');
  }
  return t('sched.stPaused');
}

// ---- the list ---------------------------------------------------------------

let _doneOpen = false;

/** @param {Row} s @param {Data} data @param {() => void} refresh */
function rowEl(s, data, refresh) {
  const st = statusOf(s, data.firing);
  const row = el('div', `lv-sched-row lv-${st}`);
  row.tabIndex = 0;
  row.setAttribute('role', 'link');
  row.setAttribute('aria-label', displayName(s));

  const main = el('div', 'lv-sched-row-main');
  main.append(el('div', 'lv-sched-row-name', displayName(s)));
  const line = el('div', 'lv-sched-row-line');
  line.append(el('span', '', describeTrigger(s)));
  const now = standing(s, st);
  if (now) {
    const where = el('span', `lv-sched-standing lv-${st}`, now);
    if (st === 'active') where.title = formatDateTime(s.next_fire);
    line.append(el('span', 'lv-sched-sep', '·'), where);
  }
  const live = data.firing.get(s.id);
  if (st === 'running' && live) {
    const watch = button(t('sched.watch'), null, () => openChat(live), 'lv-sched-link');
    watch.addEventListener('click', (e) => e.stopPropagation()); // not the row's click
    line.append(watch);
  }
  main.append(line);
  const facts = extraFacts(s);
  if (facts.length) main.append(el('div', 'lv-sched-row-facts', facts.join(' · ')));

  const side = el('div', 'lv-sched-row-side');
  if (s.last_status === 'ok' || s.last_status === 'error') {
    const ok = s.last_status === 'ok';
    const last = el('span', `lv-sched-last lv-${ok ? 'ok' : 'error'}`);
    last.innerHTML = icon(ok ? 'check' : 'x', { size: 13 });
    last.title = ok ? t('sched.lastOk') : t('sched.lastError', { error: s.last_error || '' });
    side.append(last);
  }
  if (!isDone(s)) {
    side.append(toggle(s.active, s.active ? t('sched.pause') : t('sched.resume'), (on) => setActive(s, on, refresh)));
  }
  const more = /** @type {HTMLButtonElement} */ (el('button', 'lv-btn-icon lv-sched-more'));
  more.type = 'button';
  more.innerHTML = icon('ellipsis', { size: 16 });
  more.setAttribute('aria-label', t('sched.more'));
  more.setAttribute('aria-haspopup', 'menu');
  more.addEventListener('click', (e) => {
    e.stopPropagation();
    moreMenu(more, s, refresh);
  });
  side.append(more);

  row.append(dot(st), main, side);
  const open = () => openView(`${VIEW}/${s.id}`);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => {
    if (e.target === row && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      open();
    }
  });
  return row;
}

/** Ready-made starting points for an empty page. */
function templates() {
  const tomorrow9 = new Date();
  tomorrow9.setDate(tomorrow9.getDate() + 1);
  tomorrow9.setHours(9, 0, 0, 0);
  return [
    {
      name: t('sched.tplDigestName'),
      input: t('sched.tplDigestInput'),
      trigger_kind: 'cron',
      trigger_expr: '0 9 * * 1-5',
    },
    {
      name: t('sched.tplWatchName'),
      input: t('sched.tplWatchInput'),
      trigger_kind: 'every',
      trigger_expr: '1800',
      until: t('sched.tplWatchUntil'),
      max_fires: 24,
    },
    {
      name: t('sched.tplRemindName'),
      input: t('sched.tplRemindInput'),
      trigger_kind: 'at',
      trigger_expr: String(Math.floor(tomorrow9.getTime() / 1000)),
    },
  ];
}

function emptyState() {
  const wrap = el('div', 'lv-sched-empty');
  wrap.innerHTML = icon('clock', { size: 28 });
  wrap.append(
    el('h3', 'lv-sched-empty-title', t('sched.none')),
    el('p', 'lv-sched-empty-text', t('sched.noneHint')),
  );
  const list = el('div', 'lv-sched-templates');
  for (const tpl of templates()) {
    const card = el('button', 'lv-sched-template');
    card.setAttribute('type', 'button');
    card.append(
      el('span', 'lv-sched-template-name', tpl.name),
      el('span', 'lv-sched-template-when', describeTrigger(/** @type {any} */ (tpl))),
    );
    card.addEventListener('click', () => openScheduleForm({ preset: tpl }));
    list.append(card);
  }
  wrap.append(list, el('p', 'lv-sched-empty-tip', t('sched.chatTip')));
  return wrap;
}

/** @param {Data} data @param {() => void} refresh */
function listPage(data, refresh) {
  const page = el('div', 'lv-sched-page');
  const head = el('header', 'lv-sched-head');
  head.append(
    el('h2', 'lv-sched-title', t('sched.title')),
    button(t('sched.new'), 'plus', () => openScheduleForm({}), 'lv-btn lv-btn-primary lv-btn-sm'),
  );
  page.append(head);
  if (!data.rows.length) {
    page.append(emptyState());
    return page;
  }
  /** @type {Record<string, Row[]>} */
  const groups = { active: [], paused: [], done: [] };
  for (const s of data.rows) {
    const st = statusOf(s, data.firing);
    groups[st === 'running' ? 'active' : st].push(s);
  }
  // Soonest first while it runs; the rest newest first, as listed.
  groups.active.sort((a, b) => a.next_fire - b.next_fire);
  for (const key of ['active', 'paused']) {
    if (!groups[key].length) continue;
    const section = el('section', 'lv-sched-group');
    const h = el('h3', 'lv-sched-group-head');
    h.append(el('span', '', t(`sched.group.${key}`)), el('span', 'lv-sched-count', String(groups[key].length)));
    section.append(h, ...groups[key].map((s) => rowEl(s, data, refresh)));
    page.append(section);
  }
  if (groups.done.length) {
    const details = /** @type {HTMLDetailsElement} */ (el('details', 'lv-sched-group lv-sched-done'));
    details.open = _doneOpen;
    details.addEventListener('toggle', () => {
      _doneOpen = details.open;
    });
    const summary = el('summary', 'lv-sched-group-head');
    summary.innerHTML = icon('chevron-right', { size: 14 });
    summary.append(el('span', '', t('sched.group.done')), el('span', 'lv-sched-count', String(groups.done.length)));
    details.append(summary, ...groups.done.map((s) => rowEl(s, data, refresh)));
    page.append(details);
  }
  return page;
}

// ---- a schedule ----------------------------------------------------------------

/** @param {any} r a run record */
function runEl(r) {
  const line = el('div', `lv-sched-run lv-${r.status}`);
  const glyph = {
    completed: 'check',
    failed: 'x',
    skipped: 'minus',
    cancelled: 'square',
    interrupted: 'pause',
    running: 'loader-circle',
  }[r.status] || 'minus';
  const mark = el('span', 'lv-sched-run-mark');
  mark.innerHTML = icon(glyph, { size: 13 });
  const when = el('span', 'lv-sched-run-when', formatWhen(r.started_at));
  when.title = formatDateTime(r.started_at, { seconds: true });
  const label = el('span', 'lv-sched-run-status', t(`sched.run.${r.status}`) || r.status);
  const detail = el('span', 'lv-sched-run-detail');
  if (r.error) {
    detail.textContent = r.error;
    detail.title = r.error;
  } else {
    const bits = [];
    if (r.finished_at && r.finished_at > r.started_at) {
      const secs = Math.round(r.finished_at - r.started_at);
      bits.push(secs >= 90 ? t('sched.mins', { n: Math.round(secs / 60) }) : t('sched.secs', { n: secs }));
    }
    if (r.usage?.total_tokens) bits.push(t('sched.tokens', { n: r.usage.total_tokens.toLocaleString() }));
    detail.textContent = bits.join(' · ');
  }
  line.append(mark, when, label, detail);
  if (r.session_id) {
    line.append(button(t('sched.openChat'), null, () => openChat(r.session_id), 'lv-sched-link'));
  }
  return line;
}

/**
 * @param {Row} s @param {Data} data @param {any[] | null} history
 * @param {{ refresh: () => void, more: () => void, limit: number }} ctx
 */
function detailPage(s, data, history, ctx) {
  const st = statusOf(s, data.firing);
  const page = el('div', 'lv-sched-page lv-sched-detail');

  const back = /** @type {HTMLAnchorElement} */ (el('a', 'lv-sched-back'));
  back.href = `?view=${VIEW}`;
  back.innerHTML = icon('chevron-left', { size: 14 });
  back.append(el('span', '', t('sched.title')));

  const head = el('header', 'lv-sched-head');
  const titles = el('div', 'lv-sched-head-text');
  titles.append(el('h2', 'lv-sched-title', displayName(s)));
  const sub = el('p', 'lv-sched-sub');
  const now = standing(s, st);
  sub.append(dot(st), el('span', '', now ? `${describeTrigger(s)} · ${now}` : describeTrigger(s)));
  titles.append(sub);
  const actions = el('div', 'lv-sched-actions');
  actions.append(button(t('sched.runNow'), 'play', () => runNow(s, ctx.refresh)));
  if (!isDone(s)) {
    const label = el('label', 'lv-sched-switch');
    label.append(
      el('span', '', s.active ? t('sched.stActive') : t('sched.stPaused')),
      toggle(s.active, s.active ? t('sched.pause') : t('sched.resume'), (on) => setActive(s, on, ctx.refresh)),
    );
    actions.append(label);
  }
  const more = /** @type {HTMLButtonElement} */ (el('button', 'lv-btn-icon'));
  more.type = 'button';
  more.innerHTML = icon('ellipsis', { size: 16 });
  more.setAttribute('aria-label', t('sched.more'));
  more.setAttribute('aria-haspopup', 'menu');
  more.addEventListener('click', () => moreMenu(more, s, ctx.refresh, { run: false }));
  actions.append(more);
  head.append(titles, actions);
  page.append(back, head);

  if (st === 'running' && data.firing.get(s.id)) {
    const live = /** @type {string} */ (data.firing.get(s.id));
    const note = el('div', 'lv-sched-note');
    note.append(el('span', '', t('sched.runningNote')), button(t('sched.watch'), null, () => openChat(live), 'lv-sched-link'));
    page.append(note);
  } else if (st === 'done') {
    const note = el('div', 'lv-sched-note');
    const why = s.finished_reason ? finishedReason(s.finished_reason) : t('sched.onceDone');
    note.append(el('span', '', t('sched.endedNote', { why })));
    if (s.finished_reason === 'max fires reached') note.append(el('span', 'lv-sched-note-hint', t('sched.maxFiresHint')));
    note.append(button(t('sched.editToResume'), 'pencil', () => openScheduleForm({ editing: s, onSaved: ctx.refresh }), 'lv-sched-link'));
    page.append(note);
  }

  // The instruction, folded past a dozen lines.
  const prompt = el('section', 'lv-sched-section');
  prompt.append(el('h3', 'lv-sched-section-title', t('sched.instruction')));
  const text = el('div', 'lv-sched-prompt', s.input);
  prompt.append(text);
  requestAnimationFrame(() => {
    if (text.scrollHeight <= text.clientHeight + 2) return;
    const expand = button(t('sched.showAll'), null, () => {
      text.classList.add('lv-open');
      expand.remove();
    }, 'lv-sched-link');
    prompt.append(expand);
  });
  page.append(prompt);

  const settings = el('section', 'lv-sched-section');
  settings.append(el('h3', 'lv-sched-section-title', t('sched.settings')));
  const facts = el('dl', 'lv-sched-facts');
  /** @param {string} label @param {string | Node} value @param {string} [title] */
  const fact = (label, value, title) => {
    const dd = el('dd');
    dd.append(value);
    if (title) dd.title = title;
    facts.append(el('dt', '', label), dd);
  };
  const raw = s.trigger_kind === 'at' ? formatDateTime(Number(s.trigger_expr)) : `${s.trigger_kind} ${s.trigger_expr}`;
  fact(t('sched.trigger'), describeTrigger(s), raw);
  if (workspacesOn() || s.workspace_mode) {
    const where = [];
    if (workspacesOn()) where.push(s.workspace_root ? workspaceName(s.workspace_root) : t('sched.ownDir'));
    if (s.workspace_mode) where.push(modeLabel(s.workspace_mode));
    fact(t('sched.where'), where.join(' · '), s.workspace_root ?? undefined);
  }
  if (s.session_id) {
    const sid = s.session_id;
    const link = button(t('sched.continuesShort', { title: s.session_title || t('session.newChat') }), null, () => openChat(sid), 'lv-sched-link');
    fact(t('sched.results'), link);
  } else {
    fact(t('sched.results'), t('sched.freshChat'));
  }
  if (store.agents.length > 1 && s.agent) fact(t('sched.agentLabel'), s.agent);
  if (s.until) fact(t('sched.untilLabel'), s.until);
  const nets = [];
  if (s.max_fires != null) nets.push(t('sched.firesOf', { n: s.fire_count ?? 0, m: s.max_fires }));
  if (s.expires_at != null) nets.push(t('sched.expiresAt', { time: formatDateTime(s.expires_at) }));
  if (nets.length) fact(t('sched.safetyNet'), nets.join(' · '));
  fact(t('sched.created'), formatDateTime(s.created_at));
  settings.append(facts);
  page.append(settings);

  const runs = el('section', 'lv-sched-section');
  runs.append(el('h3', 'lv-sched-section-title', t('sched.history')));
  if (history === null) {
    runs.append(el('p', 'lv-sched-muted', t('sched.historyFailed')));
  } else if (!history.length) {
    runs.append(el('p', 'lv-sched-muted', t('sched.historyNone')));
  } else {
    const list = el('div', 'lv-sched-runs');
    list.append(...history.map(runEl));
    runs.append(list);
    if (history.length >= ctx.limit && ctx.limit < 200) {
      runs.append(button(t('sched.loadMore'), null, ctx.more, 'lv-btn lv-btn-ghost lv-btn-sm lv-sched-more-runs'));
    }
  }
  page.append(runs);
  return page;
}

// ---- the view -------------------------------------------------------------------

/** @param {HTMLElement} root @param {{ path: string }} ctx */
function render(root, { path }) {
  const id = path || null;
  let alive = true;
  let limit = HISTORY_PAGE;
  let timer = 0;
  const scroller = document.getElementById('view');

  /** @param {Node} node */
  const paint = (node) => {
    const top = scroller?.scrollTop ?? 0;
    root.replaceChildren(node);
    if (scroller) scroller.scrollTop = top;
  };

  async function draw() {
    let data;
    try {
      data = await load();
    } catch {
      if (alive && !root.childElementCount) paint(el('p', 'lv-sched-muted lv-sched-page', t('sched.loadFailed')));
      return;
    }
    if (!alive) return;
    if (!id) {
      paint(listPage(data, refresh));
      return;
    }
    const s = data.rows.find((r) => r.id === id);
    if (!s) {
      openView(VIEW); // deleted, here or elsewhere
      return;
    }
    const history = await api.scheduleRuns(id, { limit }).catch(() => null);
    if (!alive) return;
    paint(
      detailPage(s, data, history, {
        refresh,
        limit,
        more: () => {
          limit = Math.min(200, limit + HISTORY_PAGE);
          refresh();
        },
      }),
    );
  }
  function refresh() {
    clearTimeout(timer);
    timer = setTimeout(draw, 150); // coalesce event bursts
  }

  const offRuns = store.on('runs-changed', refresh);
  const offSaved = store.on('schedules-changed', refresh);
  draw();
  return () => {
    alive = false;
    clearTimeout(timer);
    offRuns();
    offSaved();
  };
}

// ---- the form -----------------------------------------------------------------

/** @typedef {'once' | 'daily' | 'interval' | 'cron'} Tab */

/**
 * A strip of a few fixed choices (the Files panel's look).
 * @param {[string, string][]} options value, label
 * @param {string} value
 * @param {(value: string) => void} onChange
 * @param {string} label
 */
function tabs(options, value, onChange, label) {
  const strip = el('div', 'lv-files-tabs');
  strip.setAttribute('role', 'tablist');
  strip.setAttribute('aria-label', label);
  /** @type {HTMLButtonElement[]} */
  const buttons = [];
  const select = (/** @type {string} */ v) => {
    for (const b of buttons) {
      const on = b.dataset.value === v;
      b.classList.toggle('lv-active', on);
      b.setAttribute('aria-selected', String(on));
    }
  };
  for (const [v, text] of options) {
    const b = /** @type {HTMLButtonElement} */ (el('button', 'lv-files-tab', text));
    b.type = 'button';
    b.dataset.value = v;
    b.setAttribute('role', 'tab');
    b.addEventListener('click', () => {
      select(v);
      onChange(v);
    });
    buttons.push(b);
    strip.append(b);
  }
  select(value);
  return Object.assign(strip, { select });
}

/** @param {number} epoch seconds → a datetime-local value */
function localInput(epoch) {
  const d = new Date(epoch * 1000);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (/** @type {number} */ n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** @param {string} value a datetime-local value → epoch seconds, or NaN */
function fromLocalInput(value) {
  return value ? Math.floor(new Date(value).getTime() / 1000) : NaN;
}

/**
 * @param {string} labelText @param {HTMLElement} control @param {string} [hint]
 */
function field(labelText, control, hint) {
  const wrap = el('div', 'lv-sched-field');
  const label = /** @type {HTMLLabelElement} */ (el('label', 'lv-sched-label', labelText));
  if (control.matches('input, textarea, select')) {
    control.id ||= `sched-field-${Math.random().toString(36).slice(2, 8)}`;
    label.htmlFor = control.id;
  }
  wrap.append(label, control);
  if (hint) wrap.append(el('div', 'lv-sched-hint', hint));
  return wrap;
}

/** @param {string} type @param {string} [cls] */
function input(type, cls = '') {
  const n = /** @type {HTMLInputElement} */ (el('input', `lv-dialog-input ${cls}`.trim()));
  n.type = type;
  return n;
}

/**
 * A chip that opens a menu of choices (the composer's folder and mode chips).
 * @param {string} label
 */
function chip(label) {
  const b = /** @type {HTMLButtonElement} */ (el('button', 'lv-sched-chip'));
  b.type = 'button';
  b.setAttribute('aria-haspopup', 'menu');
  b.setAttribute('aria-label', label);
  return b;
}

/**
 * Create a schedule, or edit one. `preset` fills a new one (a template, a
 * copy, or the chat it should continue).
 * @param {{ editing?: Row | null, preset?: Partial<Row> | null, onSaved?: () => void }} opts
 */
export function openScheduleForm({ editing = null, preset = null, onSaved } = {}) {
  const src = editing ?? preset ?? {};
  const form = /** @type {HTMLFormElement} */ (el('form', 'lv-sched-form'));
  form.noValidate = true;
  const head = el('div', 'lv-sched-form-head');
  head.append(el('h3', 'lv-sched-form-title', editing ? t('sched.editTitle') : t('sched.newTitle')));

  const name = input('text');
  name.maxLength = 200;
  name.placeholder = t('sched.namePlaceholder');
  name.value = src.name ?? '';

  const prompt = /** @type {HTMLTextAreaElement} */ (el('textarea', 'lv-dialog-input lv-sched-input'));
  prompt.rows = 6;
  prompt.placeholder = t('sched.promptPlaceholder');
  prompt.value = src.input ?? '';

  // ---- when
  const kind = src.trigger_kind;
  const expr = src.trigger_expr ?? '';
  const daily = kind === 'cron' ? parseDaily(expr) : null;
  /** @type {Tab} */
  let tab = kind === 'at' ? 'once' : kind === 'every' ? 'interval' : kind === 'cron' && !daily ? 'cron' : 'daily';

  const once = input('datetime-local');
  once.value = kind === 'at' ? localInput(Number(expr)) : '';
  const onceQuick = el('div', 'lv-sched-quick');
  /** @param {string} label @param {() => Date} at */
  const quick = (label, at) => {
    onceQuick.append(
      button(label, null, () => {
        once.value = localInput(at().getTime() / 1000);
        changed();
      }, 'lv-sched-pill'),
    );
  };
  quick(t('sched.inHour'), () => new Date(Date.now() + 3600_000));
  quick(t('sched.tomorrow9'), () => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    d.setHours(9, 0, 0, 0);
    return d;
  });

  const days = new Set(daily?.days ?? [0, 1, 2, 3, 4, 5, 6]);
  const dayChips = el('div', 'lv-sched-days');
  dayChips.setAttribute('role', 'group');
  dayChips.setAttribute('aria-label', t('sched.days'));
  const syncDays = () => {
    for (const b of dayChips.querySelectorAll('button')) {
      b.setAttribute('aria-pressed', String(days.has(Number(/** @type {HTMLElement} */ (b).dataset.day))));
    }
  };
  for (const d of WEEK) {
    const b = /** @type {HTMLButtonElement} */ (el('button', 'lv-sched-day', t(`cron.dayShort${d}`)));
    b.type = 'button';
    b.dataset.day = String(d);
    b.setAttribute('aria-label', t(`cron.day${d}`));
    b.addEventListener('click', () => {
      if (days.has(d)) days.delete(d);
      else days.add(d);
      syncDays();
      changed();
    });
    dayChips.append(b);
  }
  syncDays();
  const daysQuick = el('div', 'lv-sched-quick');
  /** @param {string} label @param {number[]} set */
  const daysPreset = (label, set) =>
    daysQuick.append(
      button(label, null, () => {
        days.clear();
        set.forEach((d) => days.add(d));
        syncDays();
        changed();
      }, 'lv-sched-pill'),
    );
  daysPreset(t('cron.everyDay'), [0, 1, 2, 3, 4, 5, 6]);
  daysPreset(t('cron.weekdays'), [1, 2, 3, 4, 5]);
  const time = input('time');
  time.step = '60';
  const pad = (/** @type {number} */ n) => String(n).padStart(2, '0');
  time.value = daily ? `${pad(daily.hour)}:${pad(daily.minute)}` : '09:00';

  const split = kind === 'every' && Number(expr) > 0 ? splitInterval(Number(expr)) : { n: 30, unit: /** @type {const} */ ('minute') };
  const every = input('number');
  every.min = '1';
  every.step = '1';
  every.value = String(split.n);
  let unit = split.unit;
  /** @type {[string, string][]} */
  const unitOptions = [
    ['minute', t('sched.unit.minute')],
    ['hour', t('sched.unit.hour')],
    ['day', t('sched.unit.day')],
  ];
  if (unit === 'second') unitOptions.unshift(['second', t('sched.unit.second')]);
  const units = tabs(unitOptions, unit, (v) => {
    unit = /** @type {any} */ (v);
    changed();
  }, t('sched.unit'));

  const cron = input('text', 'lv-mono');
  cron.placeholder = '0 9 * * 1-5';
  cron.value = kind === 'cron' && !daily ? expr : '';
  cron.spellcheck = false;

  const panels = /** @type {Record<Tab, HTMLElement>} */ ({
    once: el('div', 'lv-sched-panel'),
    daily: el('div', 'lv-sched-panel'),
    interval: el('div', 'lv-sched-panel'),
    cron: el('div', 'lv-sched-panel'),
  });
  panels.once.append(once, onceQuick);
  const dailyRow = el('div', 'lv-sched-inline');
  dailyRow.append(el('span', 'lv-sched-inline-label', t('sched.at')), time);
  panels.daily.append(dayChips, daysQuick, dailyRow);
  const everyRow = el('div', 'lv-sched-inline');
  everyRow.append(el('span', 'lv-sched-inline-label', t('sched.everyLabel')), every, units);
  panels.interval.append(everyRow);
  panels.cron.append(cron, el('div', 'lv-sched-hint', t('sched.hintCron')));
  const panelHost = el('div', 'lv-sched-panels');
  panelHost.append(...Object.values(panels));
  const showPanel = () => {
    for (const [k, p] of Object.entries(panels)) p.hidden = k !== tab;
  };
  showPanel();
  const kinds = tabs(
    [
      ['once', t('sched.tab.once')],
      ['daily', t('sched.tab.daily')],
      ['interval', t('sched.tab.interval')],
      ['cron', t('sched.tab.cron')],
    ],
    tab,
    (v) => {
      // Carry a daily pick over into the Cron tab, so it starts from it.
      if (v === 'cron' && !cron.value.trim() && tab === 'daily') {
        try {
          cron.value = trigger().expr;
        } catch {
          /* nothing valid to carry */
        }
      }
      tab = /** @type {Tab} */ (v);
      showPanel();
      changed();
    },
    t('sched.trigger'),
  );
  const preview = el('div', 'lv-sched-preview');
  preview.setAttribute('aria-live', 'polite');
  const when = el('div', 'lv-sched-when');
  when.append(kinds, panelHost, preview);

  /** The trigger as the API takes it; throws with what to fix. */
  function trigger() {
    if (tab === 'once') {
      const epoch = fromLocalInput(once.value);
      if (!Number.isFinite(epoch)) throw new Error(t('sched.pickTime'));
      return { kind: 'at', expr: String(epoch) };
    }
    if (tab === 'daily') {
      if (!days.size) throw new Error(t('sched.pickDays'));
      const [h, m] = time.value.split(':').map(Number);
      if (!Number.isInteger(h) || !Number.isInteger(m)) throw new Error(t('sched.pickTime'));
      return { kind: 'cron', expr: dailyCron({ days: [...days], hour: h, minute: m }) };
    }
    if (tab === 'interval') {
      const n = Number(every.value);
      if (!Number.isInteger(n) || n < 1) throw new Error(t('sched.pickInterval'));
      return { kind: 'every', expr: String(n * unitSeconds(unit)) };
    }
    const value = cron.value.trim();
    if (!value) throw new Error(t('sched.enterExpr'));
    return { kind: 'cron', expr: value };
  }

  let previewSeq = 0;
  let previewTimer = 0;
  function changed() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      const seq = ++previewSeq;
      let trig;
      try {
        trig = trigger();
      } catch (err) {
        preview.className = 'lv-sched-preview lv-muted';
        preview.textContent = err.message;
        return;
      }
      const human = trig.kind === 'cron' && tab === 'cron' ? humanizeCron(trig.expr) : null;
      try {
        const res = await api.previewSchedule(trig.kind, trig.expr);
        if (seq !== previewSeq) return;
        const fires = res.next_fires.map((/** @type {number} */ f) => formatWhen(f)).join(' · ');
        let text = human ? `${human} — ${t('sched.upcoming', { fires })}` : t('sched.upcoming', { fires });
        if (trig.kind === 'at' && res.next_fires[0] * 1000 < Date.now()) text = t('sched.inPast');
        // Cron fields match the server's clock: say so where this page's differs.
        const here = -new Date().getTimezoneOffset() * 60;
        if (trig.kind === 'cron' && res.utc_offset !== here) {
          const h = res.utc_offset / 3600;
          text += ` ${t('sched.serverTz', { tz: `UTC${h >= 0 ? '+' : ''}${h}` })}`;
        }
        preview.className = 'lv-sched-preview';
        preview.textContent = text;
      } catch (err) {
        if (seq !== previewSeq) return;
        preview.className = 'lv-sched-preview lv-error';
        preview.textContent = err.message || t('sched.invalidTrigger');
      }
    }, 300);
  }
  for (const n of [once, time, every, cron]) n.addEventListener('input', changed);

  // ---- where
  const choices = workspaceChoices();
  const modes = modeChoices();
  const where = {
    root: editing ? editing.workspace_root ?? null : src.workspace_root ?? scopeRoot() ?? null,
    mode: editing ? editing.workspace_mode ?? null : src.workspace_mode ?? modes?.initial ?? null,
    touched: false,
  };
  const whereRow = el('div', 'lv-sched-chips');
  const rootChip = chip(t('sched.folder'));
  const modeChip = chip(t('sched.mode'));
  const drawWhere = () => {
    rootChip.innerHTML = icon('folder', { size: 14 });
    rootChip.append(el('span', '', where.root ? workspaceName(where.root) : t('sched.ownDir')));
    rootChip.title = where.root ?? t('sched.ownDirTitle');
    modeChip.innerHTML = modeGlyph(where.mode);
    modeChip.append(el('span', '', modeLabel(where.mode)));
  };
  rootChip.addEventListener('click', () => {
    /** @type {(import('./ui.js').MenuItem | null)[]} */
    const items = choices.map((/** @type {any} */ w) => ({
      label: workspaceName(w.root),
      icon: icon('folder', { size: 15 }),
      checked: w.root === where.root,
      onSelect: () => {
        Object.assign(where, { root: w.root, touched: true });
        drawWhere();
      },
    }));
    if (canAddWorkspace()) {
      items.push(null, {
        label: t('ws.add'),
        icon: icon('plus', { size: 15 }),
        onSelect: async () => {
          const added = await addWorkspaceDialog();
          if (added) {
            Object.assign(where, { root: added.root, touched: true });
            drawWhere();
          }
        },
      });
    }
    openMenu(rootChip, items, { align: 'start' });
  });
  modeChip.addEventListener('click', () => {
    openMenu(
      modeChip,
      (modes?.modes ?? []).map((m) => ({
        label: modeLabel(m),
        icon: modeGlyph(m),
        checked: m === where.mode,
        onSelect: () => {
          Object.assign(where, { mode: m, touched: true });
          drawWhere();
        },
      })),
      { align: 'start' },
    );
  });
  if (workspacesOn()) whereRow.append(rootChip);
  if (modes) whereRow.append(modeChip);
  drawWhere();
  const whereField = whereRow.childElementCount
    ? field(t('sched.where'), whereRow, t('sched.unattended'))
    : null;

  // ---- results: only a schedule tied to a chat has a choice to make
  const boundTo = src.session_id ?? null;
  let continues = !!boundTo;
  const results = boundTo
    ? tabs(
        [
          ['fresh', t('sched.freshChat')],
          ['continue', t('sched.continuesShort', { title: src.session_title || t('session.newChat') })],
        ],
        continues ? 'continue' : 'fresh',
        (v) => {
          continues = v === 'continue';
          if (whereField) whereField.hidden = continues;
        },
        t('sched.results'),
      )
    : null;

  // ---- advanced
  const until = input('text');
  until.maxLength = 2000;
  until.placeholder = t('sched.untilPlaceholder');
  until.value = src.until ?? '';
  const maxFires = input('number');
  maxFires.min = '1';
  maxFires.step = '1';
  maxFires.placeholder = t('sched.unlimited');
  maxFires.value = src.max_fires != null ? String(src.max_fires) : '';
  const expires = input('datetime-local');
  expires.value = src.expires_at != null ? localInput(src.expires_at) : '';
  let agent = src.agent ?? store.agent ?? store.agents[0]?.name ?? null;
  const agentChip = chip(t('sched.agentLabel'));
  const drawAgent = () => {
    agentChip.innerHTML = icon('bot', { size: 14 });
    agentChip.append(el('span', '', agent ?? ''));
  };
  agentChip.addEventListener('click', () => {
    openMenu(
      agentChip,
      store.agents.map((/** @type {any} */ a) => ({
        label: a.name,
        checked: a.name === agent,
        onSelect: () => {
          agent = a.name;
          drawAgent();
        },
      })),
      { align: 'start' },
    );
  });
  drawAgent();
  const advanced = /** @type {HTMLDetailsElement} */ (el('details', 'lv-sched-advanced'));
  advanced.open = !!(src.until || src.max_fires != null || src.expires_at != null);
  const summary = el('summary', '');
  summary.innerHTML = icon('chevron-right', { size: 14 });
  summary.append(el('span', '', t('sched.advanced')));
  const nets = el('div', 'lv-sched-pair');
  nets.append(field(t('sched.maxFires'), maxFires), field(t('sched.expires'), expires));
  advanced.append(summary, field(t('sched.untilLabel'), until, t('sched.untilHint')), nets);
  if (store.agents.length > 1) advanced.append(field(t('sched.agentLabel'), agentChip));

  // ---- foot
  const foot = el('div', 'lv-sched-form-foot');
  const cancel = button(t('sched.cancel'), null, () => dialog.close(), 'lv-btn lv-btn-ghost lv-btn-sm');
  const submit = /** @type {HTMLButtonElement} */ (el('button', 'lv-btn lv-btn-primary lv-btn-sm', editing ? t('sched.save') : t('sched.add')));
  submit.type = 'submit';
  foot.append(cancel, submit);

  form.append(
    head,
    field(t('sched.name'), name),
    field(t('sched.instruction'), prompt),
    field(t('sched.trigger'), when),
  );
  // A continued chat works where it always has: the folder and mode are a
  // fresh chat's.
  if (results) form.append(field(t('sched.results'), results));
  if (whereField) {
    whereField.hidden = continues;
    form.append(whereField);
  }
  form.append(advanced, foot);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const message = prompt.value.trim();
    if (!message) {
      toast(t('sched.needInstruction'), { type: 'error' });
      prompt.focus();
      return;
    }
    let trig;
    try {
      trig = trigger();
    } catch (err) {
      toast(err.message, { type: 'error' });
      return;
    }
    const cap = maxFires.value.trim();
    if (cap && !(Number.isInteger(Number(cap)) && Number(cap) >= 1)) {
      advanced.open = true;
      maxFires.reportValidity();
      return;
    }
    const expiry = fromLocalInput(expires.value);
    /** @type {Record<string, unknown>} */
    const body = {
      name: name.value.trim() || null,
      input: message,
      trigger_kind: trig.kind,
      trigger_expr: trig.expr,
      until: until.value.trim() || null,
      max_fires: cap ? Number(cap) : null,
      expires_at: Number.isFinite(expiry) ? expiry : null,
    };
    // Saving an ended schedule runs it again — what its page's "Edit to run
    // again" offers — once nothing would end it again at once.
    if (editing && isDone(editing)) {
      const stop =
        (trig.kind === 'at' && Number(trig.expr) * 1000 <= Date.now() && t('sched.inPast')) ||
        (body.max_fires != null && (editing.fire_count ?? 0) >= Number(body.max_fires) && t('sched.maxFiresHint')) ||
        (body.expires_at != null && Number(body.expires_at) * 1000 <= Date.now() && t('sched.expiresPast'));
      if (stop) {
        advanced.open = trig.kind !== 'at';
        toast(stop, { type: 'error' });
        return;
      }
      body.active = true;
    }
    if (store.agents.length > 1 && agent) body.agent = agent;
    if (boundTo) body.session_id = continues ? boundTo : null;
    // An edit sends where it works only when changed: an unchanged one is
    // left as stored, even where this server would no longer offer it.
    if ((!editing || where.touched) && !continues) {
      if (workspacesOn() && where.root) body.workspace_root = where.root;
      else if (editing && workspacesOn()) body.workspace_root = null;
      if (modes && where.mode) body.workspace_mode = where.mode;
    }
    submit.disabled = true;
    try {
      let saved;
      if (editing) {
        saved = await api.updateSchedule(editing.id, body);
      } else {
        for (const k of Object.keys(body)) if (body[k] === null) delete body[k];
        // A schedule is a deferred message: its fires get the page's context.
        saved = await api.createSchedule({ ...body, context: collectContext() });
      }
      dialog.close();
      store.emit('schedules-changed');
      if (editing) onSaved?.();
      else openView(`${VIEW}/${saved.id}`);
    } catch (err) {
      toast(err.message || t('sched.saveFailed'), { type: 'error' });
    } finally {
      submit.disabled = false;
    }
  });

  const dialog = showDialog({ body: form });
  dialog.classList.add('lv-dialog-wide', 'lv-sched-dialog');
  changed();
  (src.input ? name : prompt).focus();
  return dialog;
}

// ---- the open chat's schedules (topbar) ----------------------------------------

/** chat id → the schedules that continue it. */
let _byChat = new Map();
let _chipTimer = 0;

function drawChatChip() {
  const btn = /** @type {HTMLButtonElement | null} */ (document.getElementById('chat-schedules-btn'));
  if (!btn) return;
  const mine = (store.sessionId && _byChat.get(store.sessionId)) || [];
  btn.hidden = !mine.length;
  if (!mine.length) return;
  btn.innerHTML = icon('clock', { size: 14 });
  btn.append(el('span', '', mine.length === 1 ? displayName(mine[0]) : t('sched.chatCount', { n: mine.length })));
  btn.title = t('sched.chatCountTitle');
  btn.onclick = () => openView(mine.length === 1 ? `${VIEW}/${mine[0].id}` : VIEW);
}

async function loadChatSchedules() {
  try {
    /** @type {Row[]} */
    const rows = await api.listSchedules();
    _byChat = new Map();
    for (const s of rows) {
      if (!s.session_id || isDone(s)) continue;
      _byChat.set(s.session_id, [...(_byChat.get(s.session_id) ?? []), s]);
    }
  } catch {
    return; // keep what the chip showed
  }
  drawChatChip();
}

function reloadChatSchedulesSoon() {
  clearTimeout(_chipTimer);
  _chipTimer = setTimeout(loadChatSchedules, 300);
}

/** Serve the page: on a server with scheduling (main.js, from /api/info). */
export function enableSchedules() {
  registerBuiltinView(VIEW, {
    get title() {
      return t('sched.title');
    },
    render,
  });
  document.getElementById('schedules-btn')?.classList.remove('lv-hidden');
  store.canSchedule = true;
  // The chat menu's "schedule in this chat" (sessions.js).
  // Its runs continue that chat, so they take its agent — not the one the
  // page has open (the menu may be a sidebar row's).
  store.on('schedule-chat', (/** @type {{ id: string, title?: string, agent?: string | null }} */ chat) =>
    openScheduleForm({
      preset: { session_id: chat.id, session_title: chat.title, agent: chat.agent ?? null },
    }),
  );
  store.on('session-id', drawChatChip);
  store.on('schedules-changed', reloadChatSchedulesSoon);
  // A schedule_run approved in a chat shows up once its run ends.
  store.on('runs-changed', reloadChatSchedulesSoon);
  loadChatSchedules();
}
