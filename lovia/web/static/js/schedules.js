// Scheduled runs: a master-detail modal over /api/schedules, opened from the
// clock button in the topbar (shown only when the server advertises the
// `scheduling` feature). Three views, shown one at a time:
//   list   — each schedule as a calm row: status dot, prompt, meta line, and a
//            pause/resume switch (the one high-frequency action); click a row
//            to drill in.
//   detail — the full prompt, trigger/stop-condition facts, every action
//            (run now / pause / edit / delete), and the fire history.
//   form   — create or edit, with room to write a real prompt.
// While the dialog is open it re-fetches on the `runs-changed` store event
// (relayed from the /api/events stream by sessions.js), so a firing schedule
// shows a live "running" state that resolves into its outcome by itself.
import { t } from './i18n.js';
import { api } from './api.js';
import { store } from './store.js';
import { showDialog, confirmDialog } from './ui.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { formatDateTime } from './util.js';
import { switchSession } from './sessions.js';

function humanizeEvery(expr) {
  const secs = Number(expr);
  if (!Number.isFinite(secs)) return `every ${expr}`;
  if (secs % 3600 === 0) return t('sched.everyH', { n: secs / 3600 });
  if (secs % 60 === 0) return t('sched.everyM', { n: secs / 60 });
  return t('sched.everyS', { n: secs });
}

// ---- Cron in plain words --------------------------------------------------
// Covers the patterns people actually write (fixed minute/hour, day-of-week
// lists/ranges, */N steps, day-of-month); anything fancier returns null and
// the raw expression is shown instead. Deliberately hand-rolled: a full cron
// describer is a dependency for five lines of benefit.
const dayName = (d) => t(`cron.day${d % 7}`);

function dayLabel(field) {
  if (field === '1-5') return t('cron.weekdays');
  if (field === '0,6' || field === '6,0') return t('cron.weekends');
  const names = [];
  for (const part of field.split(',')) {
    const range = part.match(/^([0-7])-([0-7])$/);
    if (range) {
      const [from, to] = [Number(range[1]), Number(range[2])];
      if (from > to) return null;
      for (let d = from; d <= to; d++) names.push(dayName(d));
    } else if (/^[0-7]$/.test(part)) {
      names.push(dayName(Number(part)));
    } else {
      return null;
    }
  }
  return names.length ? names.join(', ') : null;
}

export function humanizeCron(expr) {
  const fields = expr.trim().split(/\s+/);
  if (fields.length !== 5) return null;
  const [min, hour, dom, month, dow] = fields;
  if (month !== '*') return null; // month constraints: show the raw expression
  const pad = (n) => String(n).padStart(2, '0');
  const isNum = (s) => /^\d{1,2}$/.test(s);
  const step = (s) => (/^\*\/\d+$/.test(s) ? s.slice(2) : null);

  if (min === '*' && hour === '*' && dom === '*' && dow === '*') {
    return t('cron.everyMinute');
  }
  if (step(min) && hour === '*' && dom === '*' && dow === '*') {
    return t('cron.everyNMin', { n: step(min) });
  }
  if (isNum(min) && step(hour) && dom === '*' && dow === '*') {
    return t('cron.everyNHours', { n: step(hour), m: pad(min) });
  }
  if (isNum(min) && hour === '*' && dom === '*' && dow === '*') {
    return t('cron.hourlyAt', { m: pad(min) });
  }
  if (isNum(min) && isNum(hour)) {
    const hm = `${pad(hour)}:${pad(min)}`;
    if (dom === '*' && dow === '*') return t('cron.dailyAt', { t: hm });
    if (dom === '*') {
      const days = dayLabel(dow);
      return days ? t('cron.daysAt', { days, t: hm }) : null;
    }
    if (isNum(dom) && dow === '*') return t('cron.monthlyAt', { d: dom, t: hm });
  }
  return null;
}

function describeTrigger(s) {
  if (s.trigger_kind === 'every') return humanizeEvery(s.trigger_expr);
  if (s.trigger_kind === 'cron') {
    return humanizeCron(s.trigger_expr) || `cron ${s.trigger_expr}`;
  }
  if (s.trigger_kind === 'at') {
    return t('sched.atTime', { time: formatDateTime(Number(s.trigger_expr)) });
  }
  return `${s.trigger_kind} ${s.trigger_expr}`;
}

// One-line format reminder per trigger kind, shown under the form.
const TRIGGER_HINTS = {
  get every() { return t('sched.hintEvery'); },
  get cron() { return t('sched.hintCron'); },
  get at() { return t('sched.hintAt'); },
};

// ---- the adaptive trigger-expression input -------------------------------
// `every` → integer seconds, `cron` → a cron string, `at` → a local datetime
// (converted to an epoch timestamp on submit).
function buildExprInput(kind) {
  const el = document.createElement('input');
  el.className = 'dialog-input sched-expr';
  if (kind === 'every') {
    el.type = 'number';
    el.min = '1';
    el.step = '1';
    el.value = '3600';
    el.placeholder = 'seconds';
  } else if (kind === 'cron') {
    el.type = 'text';
    el.placeholder = '*/5 * * * *';
  } else {
    el.type = 'datetime-local';
  }
  return el;
}

// Read the raw input back as the API's `trigger_expr` string (or throw).
function exprValue(kind, input) {
  if (kind === 'at') {
    if (!input.value) throw new Error(t('sched.pickTime'));
    const epoch = Math.floor(new Date(input.value).getTime() / 1000);
    if (!Number.isFinite(epoch)) throw new Error(t('sched.invalidDate'));
    return String(epoch);
  }
  const v = input.value.trim();
  if (!v) throw new Error(t('sched.enterExpr'));
  return v;
}

// The inverse, for editing: epoch-seconds string → datetime-local value.
function epochToLocalInput(expr) {
  const d = new Date(Number(expr) * 1000);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

// ---- display helpers ------------------------------------------------------
// A one-shot `at` that's no longer active and whose time has passed has already
// fired (or was missed): it's done, not paused. Resuming it would only re-run a
// moment in the past, so it gets neither a switch nor a resume action.
function isDone(s) {
  return (
    !s.active &&
    s.trigger_kind === 'at' &&
    Number(s.trigger_expr) * 1000 <= Date.now()
  );
}

/** Lifecycle state for display; `firing` holds ids with a live fired run. */
function statusOf(s, firing) {
  if (firing.has(s.id)) return 'running';
  if (s.active) return 'active';
  return (s.finished_reason || isDone(s)) ? 'done' : 'paused';
}

function statusLabel(st) {
  return {
    running: t('sched.stRunningNow'),
    active: t('sched.stActive'),
    paused: t('sched.paused'),
    done: t('sched.done'),
  }[st];
}

const clip = (txt, n) => (txt.length > n ? txt.slice(0, n - 1) + '…' : txt);

/** Create an element with a class and optional text content. */
function el(tag, cls = '', text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

// One line of a schedule's fire history (a persisted run record).
function runLine(r, onOpenSession) {
  const line = el('div', 'sched-run');
  const when = el('span', '', formatDateTime(r.started_at));
  const status = el('span', `sched-run-status ${r.status}`);
  status.textContent =
    {
      completed: t('sched.stCompleted'),
      failed: t('sched.failed'),
      cancelled: t('sched.stCancelled'),
      interrupted: t('sched.stInterrupted'),
      running: t('sched.stRunning'),
    }[r.status] || r.status;
  if (r.error) status.title = r.error;
  line.append(when, status);
  if (r.finished_at && r.finished_at > r.started_at) {
    const secs = r.finished_at - r.started_at;
    line.append(
      el('span', '', secs >= 90 ? `${Math.round(secs / 60)}m` : `${Math.round(secs)}s`),
    );
  }
  if (r.usage?.total_tokens) {
    line.append(el('span', '', `${r.usage.total_tokens.toLocaleString()} tok`));
  }
  if (r.session_id) {
    const link = el('button', 'sched-last-run', '↗');
    link.type = 'button';
    link.title = t('sched.lastRunTitle');
    link.addEventListener('click', () => onOpenSession(r.session_id));
    line.append(link);
  }
  return line;
}

/** Open the Schedules dialog: list, inspect, create, edit, and run schedules. */
export async function openSchedulesDialog() {
  const panel = el('div', 'schedules-panel');
  panel.innerHTML = `
    <div class="schedules-head">
      <div class="schedules-head-lead">
        <button type="button" class="btn-icon sched-back" hidden aria-label="${t('sched.back')}">${icon('chevron-left', { size: 16 })}</button>
        <h3 class="sched-title"></h3>
      </div>
      <div class="schedules-head-actions">
        <button type="button" class="btn btn-ghost btn-sm sched-new">${icon('plus', { size: 14 })}<span>${t('sched.new')}</span></button>
        <button type="button" class="btn-icon sched-close" aria-label="${t('dialog.close')}">${icon('x', { size: 16 })}</button>
      </div>
    </div>
    <div class="sched-view"></div>`;

  const backBtn = /** @type {HTMLButtonElement} */ (panel.querySelector('.sched-back'));
  const newBtn = /** @type {HTMLButtonElement} */ (panel.querySelector('.sched-new'));
  const titleEl = panel.querySelector('.sched-title');
  const viewEl = panel.querySelector('.sched-view');

  /** @type {any[]} */
  let rows = [];
  /** @type {Set<string>} */
  let firing = new Set();
  let loaded = false;
  /** @type {{name: 'list'} | {name: 'detail', id: string} | {name: 'form', editing: any}} */
  let view = { name: 'list' };

  const openSession = (sid) => {
    dialog.close();
    switchSession(sid).catch(() => {});
  };

  async function loadData() {
    const [schedules, runs] = await Promise.all([
      api.listSchedules(),
      api.listRuns().catch(() => []),
    ]);
    rows = schedules;
    firing = new Set(
      runs
        .map((r) => String(r.source || ''))
        .filter((src) => src.startsWith('schedule:'))
        .map((src) => src.slice('schedule:'.length)),
    );
    loaded = true;
  }

  // ---- list view ----------------------------------------------------------

  function rowMeta(s, st) {
    const parts = [describeTrigger(s)];
    if (st === 'running') {
      parts.push(t('sched.stRunningNow'));
    } else if (st === 'active') {
      parts.push(t('sched.next', { time: formatDateTime(s.next_fire) }));
    } else if (st === 'done') {
      parts.push(
        s.finished_reason
          ? `${t('sched.done')} — ${clip(s.finished_reason, 48)}`
          : t('sched.done'),
      );
    } else {
      parts.push(t('sched.paused'));
    }
    if (s.until && st !== 'done') {
      parts.push(t('sched.until', { cond: clip(s.until, 30) }));
    }
    return parts.join(' · ');
  }

  function rowEl(s) {
    const st = statusOf(s, firing);
    const item = el('div', `sched-item ${st}`);
    item.tabIndex = 0;
    item.setAttribute('role', 'button');

    const dot = el('span', `sched-dot ${st}`);
    dot.title = statusLabel(st);

    const main = el('div', 'sched-item-main');
    const prompt = el('div', 'sched-item-prompt', s.input);
    prompt.title = s.input;
    const meta = el('div', 'sched-item-meta', rowMeta(s, st));
    main.append(prompt, meta);

    const side = el('div', 'sched-item-side');
    // Pause/resume — the one action worth keeping on the row; everything else
    // lives in the detail view. A lapsed one-shot gets no switch (see isDone).
    if (!isDone(s)) {
      const sw = el('button', 'sched-switch');
      sw.type = 'button';
      sw.setAttribute('role', 'switch');
      sw.setAttribute('aria-checked', String(s.active));
      sw.title = s.active ? t('sched.pause') : t('sched.resume');
      sw.setAttribute('aria-label', sw.title);
      sw.append(el('span', 'sched-switch-knob'));
      sw.addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
          await api.setScheduleActive(s.id, !s.active);
        } catch (err) {
          toast(err.message || t('sched.updateFailed'), { type: 'error' });
        }
        refresh();
      });
      side.append(sw);
    }
    const chev = el('span', 'sched-chevron');
    chev.innerHTML = icon('chevron-right', { size: 14 });
    side.append(chev);

    item.append(dot, main, side);
    const open = () => {
      view = { name: 'detail', id: s.id };
      render();
    };
    item.addEventListener('click', open);
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        open();
      }
    });
    return item;
  }

  function listView() {
    const wrap = el('div', 'sched-list');
    if (!rows.length) {
      if (loaded) wrap.append(el('div', 'sched-empty', t('sched.none')));
      return wrap;
    }
    for (const s of rows) wrap.append(rowEl(s));
    return wrap;
  }

  // ---- detail view ---------------------------------------------------------

  function detailView(s) {
    const st = statusOf(s, firing);
    const wrap = el('div', 'sched-detail');

    const statusRow = el('div', 'sched-detail-status');
    const badge = el('span', `sched-badge ${st}`);
    badge.append(el('span', `sched-dot ${st}`), document.createTextNode(statusLabel(st)));
    statusRow.append(badge);
    if (st === 'done' && s.finished_reason) {
      const why = el('span', 'sched-finished-reason', s.finished_reason);
      why.title = s.finished_reason;
      statusRow.append(why);
    }

    const prompt = el('div', 'sched-detail-prompt', s.input);

    const facts = el('dl', 'sched-facts');
    const fact = (label, value, title) => {
      const dd = el('dd', '', value);
      if (title) dd.title = title;
      facts.append(el('dt', '', label), dd);
    };
    fact(t('sched.trigger'), describeTrigger(s), `${s.trigger_kind} ${s.trigger_expr}`);
    if (s.active) fact(t('sched.nextRun'), formatDateTime(s.next_fire));
    if (s.until) fact(t('sched.untilLabel'), s.until);
    const nets = [];
    if (s.max_fires != null) {
      nets.push(t('sched.firesOf', { n: s.fire_count ?? 0, m: s.max_fires }));
    }
    if (s.expires_at != null) {
      nets.push(t('sched.expiresAt', { time: formatDateTime(s.expires_at) }));
    }
    if (nets.length) fact(t('sched.safetyNet'), nets.join(' · '));
    if (store.agents.length > 1 && s.agent) fact(t('sched.agentLabel'), s.agent);

    const actions = el('div', 'sched-detail-actions');
    const act = (label, iconName, fn, cls = '') => {
      const b = el('button', `btn btn-ghost btn-sm sched-act ${cls}`);
      b.type = 'button';
      b.innerHTML = icon(iconName, { size: 14 });
      b.append(el('span', '', label));
      b.addEventListener('click', fn);
      actions.append(b);
    };
    act(t('sched.runNow'), 'zap', async () => {
      try {
        await api.runSchedule(s.id);
        toast(t('sched.fired'));
      } catch (err) {
        toast(err.message || t('sched.runFailed'), { type: 'error' });
      }
      refresh();
    });
    if (!isDone(s)) {
      act(
        s.active ? t('sched.pause') : t('sched.resume'),
        s.active ? 'pause' : 'play',
        async () => {
          try {
            await api.setScheduleActive(s.id, !s.active);
          } catch (err) {
            toast(err.message || t('sched.updateFailed'), { type: 'error' });
          }
          refresh();
        },
      );
    }
    act(t('sched.edit'), 'pencil', () => {
      view = { name: 'form', editing: s };
      render();
    });
    act(
      t('sched.delete'),
      'trash-2',
      async () => {
        if (!(await confirmDialog(t('sched.deleteConfirm')))) return;
        try {
          await api.deleteSchedule(s.id);
        } catch (err) {
          toast(err.message || t('sched.deleteFailed'), { type: 'error' });
        }
        view = { name: 'list' };
        refresh(); // a 404 just means it's already gone — the list will agree
      },
      'danger',
    );

    const history = el('div', 'sched-history');
    api
      .scheduleRuns(s.id)
      .then((runs) => {
        if (!wrap.isConnected) return; // the view moved on meanwhile
        history.replaceChildren(...runs.map((r) => runLine(r, openSession)));
        if (!runs.length) {
          history.append(el('div', 'sched-history-empty', t('sched.historyNone')));
        }
      })
      .catch(() => {
        if (!wrap.isConnected) return;
        history.append(el('div', 'sched-history-empty', t('sched.historyFailed')));
      });

    wrap.append(
      statusRow,
      prompt,
      facts,
      actions,
      el('div', 'sched-section-title', t('sched.history')),
      history,
    );
    return wrap;
  }

  // ---- form view -----------------------------------------------------------

  function formView(editing) {
    const form = el('form', 'sched-form');
    const input = /** @type {HTMLTextAreaElement} */ (
      el('textarea', 'dialog-input sched-input')
    );
    input.rows = 5;
    input.required = true;
    input.placeholder = t('sched.promptPlaceholder');

    const untilInput = /** @type {HTMLInputElement} */ (
      el('input', 'dialog-input sched-until')
    );
    untilInput.type = 'text';
    untilInput.maxLength = 2000;
    untilInput.placeholder = t('sched.untilPlaceholder');

    const row = el('div', 'sched-row');
    const agentSel = /** @type {HTMLSelectElement} */ (
      el('select', 'dialog-input sched-agent')
    );
    agentSel.hidden = true;
    agentSel.setAttribute('aria-label', t('sched.agentLabel'));
    if (store.agents.length > 1) {
      agentSel.hidden = false;
      agentSel.replaceChildren(
        ...store.agents.map((a) => {
          const opt = document.createElement('option');
          opt.value = a.name;
          opt.textContent = a.name;
          return opt;
        }),
      );
      agentSel.value = store.agent ?? store.agents[0].name;
    }
    const kindSel = /** @type {HTMLSelectElement} */ (
      el('select', 'dialog-input sched-kind')
    );
    kindSel.setAttribute('aria-label', t('sched.triggerKind'));
    for (const kind of ['every', 'cron', 'at']) {
      const opt = document.createElement('option');
      opt.value = kind;
      opt.textContent = t(`sched.${kind}`);
      kindSel.append(opt);
    }
    const exprWrap = el('span', 'sched-expr-wrap');
    row.append(agentSel, kindSel, exprWrap);

    const hintEl = el('div', 'sched-hint');
    // Live preview: while typing a cron expression, the hint line leads with
    // its plain-words reading ("weekdays at 09:00 — min hour …").
    const syncHint = () => {
      let hint = TRIGGER_HINTS[kindSel.value] || '';
      if (kindSel.value === 'cron') {
        const human = humanizeCron(exprInput.value);
        if (human) hint = `→ ${human} · ${hint}`;
      }
      hintEl.textContent = hint;
    };
    const attachExpr = (input_) => {
      input_.addEventListener('input', syncHint);
      return input_;
    };
    let exprInput = attachExpr(buildExprInput(kindSel.value));
    exprWrap.append(exprInput);
    kindSel.addEventListener('change', () => {
      exprInput = attachExpr(buildExprInput(kindSel.value));
      exprWrap.replaceChildren(exprInput);
      syncHint();
    });

    const foot = el('div', 'sched-form-foot');
    const cancelBtn = el('button', 'btn btn-ghost btn-sm', t('sched.cancel'));
    cancelBtn.type = 'button';
    const submitBtn = el(
      'button',
      'btn btn-primary btn-sm',
      editing ? t('sched.save') : t('sched.add'),
    );
    submitBtn.type = 'submit';
    foot.append(cancelBtn, submitBtn);

    const goBack = () => {
      view = editing ? { name: 'detail', id: editing.id } : { name: 'list' };
      render();
    };
    cancelBtn.addEventListener('click', goBack);

    if (editing) {
      input.value = editing.input;
      untilInput.value = editing.until || '';
      if (store.agents.length > 1 && editing.agent) agentSel.value = editing.agent;
      kindSel.value = editing.trigger_kind;
      exprInput = attachExpr(buildExprInput(editing.trigger_kind));
      exprInput.value =
        editing.trigger_kind === 'at'
          ? epochToLocalInput(editing.trigger_expr)
          : editing.trigger_expr;
      exprWrap.replaceChildren(exprInput);
    }
    syncHint();

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const message = input.value.trim();
      if (!message) return;
      let trigger_expr;
      try {
        trigger_expr = exprValue(kindSel.value, exprInput);
      } catch (err) {
        toast(err.message, { type: 'error' });
        return;
      }
      const body = { input: message, trigger_kind: kindSel.value, trigger_expr };
      if (store.agents.length > 1) body.agent = agentSel.value;
      const until = untilInput.value.trim();
      if (until) body.until = until;
      else if (editing) body.until = null; // clearing the field clears the condition
      try {
        if (editing) {
          await api.updateSchedule(editing.id, body);
          view = { name: 'detail', id: editing.id };
        } else {
          await api.createSchedule(body);
          view = { name: 'list' };
        }
        refresh();
      } catch (err) {
        toast(err.message || t('sched.saveFailed'), { type: 'error' });
      }
    });

    form.append(input, untilInput, row, hintEl, foot);
    return form;
  }

  // ---- view switching + live refresh ---------------------------------------

  function render() {
    const isList = view.name === 'list';
    backBtn.hidden = isList;
    newBtn.hidden = !isList;
    titleEl.textContent =
      view.name === 'detail'
        ? t('sched.detail')
        : view.name === 'form'
          ? view.editing
            ? t('sched.editTitle')
            : t('sched.newTitle')
          : t('sched.title');
    if (view.name === 'detail') {
      const id = view.id;
      const s = rows.find((r) => r.id === id);
      if (!s) {
        // Deleted (possibly elsewhere) — fall back to the list.
        view = { name: 'list' };
        render();
        return;
      }
      viewEl.replaceChildren(detailView(s));
    } else if (view.name === 'form') {
      viewEl.replaceChildren(formView(view.editing));
      /** @type {HTMLTextAreaElement} */ (viewEl.querySelector('.sched-input'))?.focus();
    } else {
      const prevScroll = viewEl.querySelector('.sched-list')?.scrollTop ?? 0;
      viewEl.replaceChildren(listView());
      viewEl.querySelector('.sched-list').scrollTop = prevScroll;
    }
  }

  let refreshTimer = 0;
  async function refresh() {
    try {
      await loadData();
    } catch {
      if (view.name === 'list' && !loaded) {
        viewEl.replaceChildren(el('div', 'sched-empty', t('sched.loadFailed')));
      }
      return;
    }
    if (view.name !== 'form') render(); // never clobber a form being typed in
  }
  const refreshSoon = () => {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 150); // coalesce event bursts
  };

  backBtn.addEventListener('click', () => {
    view =
      view.name === 'form' && view.editing
        ? { name: 'detail', id: view.editing.id }
        : { name: 'list' };
    render();
  });
  newBtn.addEventListener('click', () => {
    view = { name: 'form', editing: null };
    render();
  });

  const offRuns = store.on('runs-changed', refreshSoon);
  const dialog = showDialog({
    body: panel,
    onClose: () => {
      offRuns();
      clearTimeout(refreshTimer);
    },
  });
  dialog.classList.add('dialog-wide');
  panel.querySelector('.sched-close').addEventListener('click', () => dialog.close());

  render(); // paint the shell immediately…
  refresh(); // …then fill it
}

/** Wire up the Schedules button in the sidebar. */
export function initSchedules() {
  document
    .getElementById('schedules-btn')
    ?.addEventListener('click', openSchedulesDialog);
}
