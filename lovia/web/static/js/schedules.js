// Scheduled runs: a lightweight modal to list / create / pause / delete the
// cron · interval · one-shot schedules served by /api/schedules. Opened from the
// clock button in the topbar (shown only when the server advertises the
// `scheduling` feature).
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

// ---- the dialog ----------------------------------------------------------
// A one-shot `at` that's no longer active and whose time has passed has already
// fired (or was missed): it's done, not paused. Resuming it would only re-run a
// moment in the past, so we label it "done" and drop the Resume control.
function isDone(s) {
  return (
    !s.active &&
    s.trigger_kind === 'at' &&
    Number(s.trigger_expr) * 1000 <= Date.now()
  );
}

// One line of a schedule's fire history (a persisted run record).
function runLine(r, onOpenSession) {
  const line = document.createElement('div');
  line.className = 'sched-run';
  const when = document.createElement('span');
  when.textContent = formatDateTime(r.started_at);
  const status = document.createElement('span');
  status.className = `sched-run-status ${r.status}`;
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
    const dur = document.createElement('span');
    const secs = r.finished_at - r.started_at;
    dur.textContent = secs >= 90 ? `${Math.round(secs / 60)}m` : `${Math.round(secs)}s`;
    line.append(dur);
  }
  if (r.usage?.total_tokens) {
    const tok = document.createElement('span');
    tok.textContent = `${r.usage.total_tokens.toLocaleString()} tok`;
    line.append(tok);
  }
  if (r.session_id) {
    const link = document.createElement('button');
    link.type = 'button';
    link.className = 'sched-last-run';
    link.textContent = '↗';
    link.title = t('sched.lastRunTitle');
    link.addEventListener('click', () => onOpenSession(r.session_id));
    line.append(link);
  }
  return line;
}

function rowEl(s, { onChange, onEdit, onOpenSession }) {
  const done = isDone(s);
  const item = document.createElement('div');
  item.className = 'sched-item' + (s.active ? '' : ' paused');

  const main = document.createElement('div');
  main.className = 'sched-item-main';
  const prompt = document.createElement('div');
  prompt.className = 'sched-item-prompt';
  prompt.textContent = s.input;
  prompt.title = s.input;
  const meta = document.createElement('div');
  meta.className = 'sched-item-meta';
  const clip = (txt, n) => (txt.length > n ? txt.slice(0, n - 1) + '…' : txt);
  // A schedule that deactivated itself (stop condition met, expired, max
  // fires) reads as done-with-a-reason; a plain user pause stays "paused".
  const finishedReason = !s.active && s.finished_reason ? s.finished_reason : null;
  meta.textContent = s.active
    ? `${describeTrigger(s)} · ${t('sched.next', { time: formatDateTime(s.next_fire) })}`
    : `${describeTrigger(s)} · ${
        finishedReason
          ? `${t('sched.done')} — ${clip(finishedReason, 60)}`
          : done
            ? t('sched.done')
            : t('sched.paused')
      }`;
  if (s.until && !finishedReason) {
    meta.append(` · ${t('sched.until', { cond: clip(s.until, 40) })}`);
  }
  // The humanized trigger keeps the raw expression one hover away — plus the
  // full stop condition, fire budget, and finish reason when present.
  let hover = `${s.trigger_kind} ${s.trigger_expr}`;
  if (s.until) hover += `\n${t('sched.until', { cond: s.until })}`;
  if (s.max_fires) hover += `\nfires ${s.fire_count}/${s.max_fires}`;
  if (finishedReason) hover += `\n${finishedReason}`;
  meta.title = hover;
  // Outcome of the most recent fire (None until it first completes).
  if (s.last_status) {
    const status = document.createElement('span');
    status.className = `sched-status ${s.last_status}`;
    status.textContent = s.last_status === 'ok' ? '✓' : t('sched.failed');
    if (s.last_error) status.title = s.last_error;
    meta.append(' · ', status);
  }
  // Answer "where did my scheduled run go?" — jump to the last fire's chat.
  if (s.last_session_id) {
    const link = document.createElement('button');
    link.type = 'button';
    link.className = 'sched-last-run';
    link.textContent = t('sched.lastRun');
    link.title = t('sched.lastRunTitle');
    link.addEventListener('click', () => onOpenSession(s.last_session_id));
    meta.append(' · ', link);
  }
  // Fire history, folded away until asked for (loaded fresh on each open).
  const history = document.createElement('div');
  history.className = 'sched-history';
  history.hidden = true;
  main.append(prompt, meta, history);

  const actions = document.createElement('div');
  actions.className = 'sched-item-actions';
  const btn = (title, iconName, fn) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.title = title;
    b.innerHTML = icon(iconName, { size: 14 });
    b.addEventListener('click', fn);
    actions.append(b);
    return b;
  };

  btn(t('sched.runNow'), 'zap', async () => {
    try {
      await api.runSchedule(s.id);
      toast(t('sched.fired'));
    } catch (err) {
      toast(err.message || t('sched.runFailed'), { type: 'error' });
    } finally {
      onChange();
    }
  });

  // A finished one-shot can't meaningfully resume — no pause/resume for it.
  if (!done) {
    btn(s.active ? t('sched.pause') : t('sched.resume'), s.active ? 'pause' : 'play', async () => {
      try {
        await api.setScheduleActive(s.id, !s.active);
        onChange();
      } catch (err) {
        toast(err.message || t('sched.updateFailed'), { type: 'error' });
      }
    });
  }

  btn(t('sched.history'), 'history', async () => {
    if (!history.hidden) {
      history.hidden = true;
      return;
    }
    history.hidden = false;
    try {
      const runs = await api.scheduleRuns(s.id);
      history.replaceChildren(...runs.map((r) => runLine(r, onOpenSession)));
      if (!runs.length) {
        history.innerHTML = `<div class="sched-history-empty">${t('sched.historyNone')}</div>`;
      }
    } catch {
      history.innerHTML = `<div class="sched-history-empty">${t('sched.historyFailed')}</div>`;
    }
  });

  btn(t('sched.edit'), 'pencil', () => onEdit(s));

  btn(t('sched.delete'), 'x', async () => {
    if (!(await confirmDialog(t('sched.deleteConfirm')))) return;
    try {
      await api.deleteSchedule(s.id);
    } catch (err) {
      toast(err.message || t('sched.deleteFailed'), { type: 'error' });
    } finally {
      onChange(); // refresh either way — a 404 just means it's already gone
    }
  });

  item.append(main, actions);
  return item;
}

/** Open the Schedules dialog: create, edit, list, run, and delete scheduled runs. */
export async function openSchedulesDialog() {
  const panel = document.createElement('div');
  panel.className = 'schedules-panel';
  panel.innerHTML = `
    <div class="schedules-head">
      <h3>${t('sched.title')}</h3>
      <button type="button" class="btn-icon sched-close" aria-label="${t('dialog.close')}">${icon('x', { size: 16 })}</button>
    </div>
    <form class="sched-form">
      <textarea class="dialog-input sched-input" rows="2" placeholder="${t('sched.promptPlaceholder')}" required></textarea>
      <input type="text" class="dialog-input sched-until" maxlength="2000" placeholder="${t('sched.untilPlaceholder')}" />
      <div class="sched-row">
        <select class="dialog-input sched-agent" aria-label="${t('sched.agentLabel')}" hidden></select>
        <select class="dialog-input sched-kind" aria-label="${t('sched.triggerKind')}">
          <option value="every">${t('sched.every')}</option>
          <option value="cron">${t('sched.cron')}</option>
          <option value="at">${t('sched.at')}</option>
        </select>
        <span class="sched-expr-wrap"></span>
        <button type="button" class="btn btn-ghost btn-sm sched-cancel-edit" hidden>${t('sched.cancel')}</button>
        <button type="submit" class="btn btn-primary btn-sm">${t('sched.add')}</button>
      </div>
      <div class="sched-hint"></div>
    </form>
    <div class="sched-list"></div>`;

  const form = panel.querySelector('.sched-form');
  const input = /** @type {HTMLTextAreaElement} */ (panel.querySelector('.sched-input'));
  const untilInput = /** @type {HTMLInputElement} */ (panel.querySelector('.sched-until'));
  const agentSel = /** @type {HTMLSelectElement} */ (panel.querySelector('.sched-agent'));
  const kindSel = /** @type {HTMLSelectElement} */ (panel.querySelector('.sched-kind'));
  const exprWrap = panel.querySelector('.sched-expr-wrap');
  const hintEl = panel.querySelector('.sched-hint');
  const submitBtn = panel.querySelector('.sched-form [type="submit"]');
  const cancelEditBtn = /** @type {HTMLButtonElement} */ (panel.querySelector('.sched-cancel-edit'));
  const listEl = panel.querySelector('.sched-list');
  let editingId = null; // non-null → the form saves an existing schedule

  // Agent picker only when there's a choice to make.
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
  const attachExpr = (el) => {
    el.addEventListener('input', syncHint);
    return el;
  };
  let exprInput = attachExpr(buildExprInput(kindSel.value));
  exprWrap.appendChild(exprInput);
  syncHint();
  kindSel.addEventListener('change', () => {
    exprInput = attachExpr(buildExprInput(kindSel.value));
    exprWrap.replaceChildren(exprInput);
    syncHint();
  });

  function exitEditMode() {
    editingId = null;
    input.value = '';
    untilInput.value = '';
    submitBtn.textContent = t('sched.add');
    cancelEditBtn.hidden = true;
  }

  // Prefill the form from an existing row; submit then PATCHes it in place.
  function enterEditMode(s) {
    editingId = s.id;
    input.value = s.input;
    untilInput.value = s.until || '';
    if (store.agents.length > 1 && s.agent) agentSel.value = s.agent;
    kindSel.value = s.trigger_kind;
    exprInput = attachExpr(buildExprInput(s.trigger_kind));
    exprInput.value =
      s.trigger_kind === 'at' ? epochToLocalInput(s.trigger_expr) : s.trigger_expr;
    exprWrap.replaceChildren(exprInput);
    syncHint();
    submitBtn.textContent = t('sched.save');
    cancelEditBtn.hidden = false;
    input.focus();
  }

  cancelEditBtn.addEventListener('click', exitEditMode);

  async function refresh() {
    try {
      const rows = await api.listSchedules();
      if (!rows.length) {
        listEl.innerHTML = `<div class="sched-empty">${t('sched.none')}</div>`;
        return;
      }
      listEl.replaceChildren(
        ...rows.map((s) =>
          rowEl(s, {
            onChange: refresh,
            onEdit: enterEditMode,
            onOpenSession: (sid) => {
              dialog.close();
              switchSession(sid).catch(() => {});
            },
          }),
        ),
      );
    } catch (err) {
      listEl.innerHTML = `<div class="sched-empty">${t('sched.loadFailed')}</div>`;
    }
  }

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
    else if (editingId) body.until = null; // clearing the field clears the condition
    try {
      if (editingId) {
        await api.updateSchedule(editingId, body);
        exitEditMode();
      } else {
        await api.createSchedule(body);
        input.value = '';
        untilInput.value = '';
      }
      await refresh();
    } catch (err) {
      toast(err.message || t('sched.saveFailed'), { type: 'error' });
    }
  });

  const dialog = showDialog({ body: panel });
  dialog.classList.add('dialog-wide');
  panel.querySelector('.sched-close').addEventListener('click', () => dialog.close());
  refresh();
}

/** Wire up the Schedules button in the sidebar. */
export function initSchedules() {
  document
    .getElementById('schedules-btn')
    ?.addEventListener('click', openSchedulesDialog);
}
