// Scheduled runs: a lightweight modal to list / create / pause / delete the
// cron · interval · one-shot schedules served by /api/schedules. Opened from the
// clock button in the topbar (shown only when the server advertises the
// `scheduling` feature).
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
  if (secs % 3600 === 0) return `every ${secs / 3600}h`;
  if (secs % 60 === 0) return `every ${secs / 60}m`;
  return `every ${secs}s`;
}

// ---- Cron in plain words --------------------------------------------------
// Covers the patterns people actually write (fixed minute/hour, day-of-week
// lists/ranges, */N steps, day-of-month); anything fancier returns null and
// the raw expression is shown instead. Deliberately hand-rolled: a full cron
// describer is a dependency for five lines of benefit.
const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function dayLabel(field) {
  if (field === '1-5') return 'weekdays';
  if (field === '0,6' || field === '6,0') return 'weekends';
  const names = [];
  for (const part of field.split(',')) {
    const range = part.match(/^([0-7])-([0-7])$/);
    if (range) {
      const [from, to] = [Number(range[1]), Number(range[2])];
      if (from > to) return null;
      for (let d = from; d <= to; d++) names.push(DAY_NAMES[d % 7]);
    } else if (/^[0-7]$/.test(part)) {
      names.push(DAY_NAMES[Number(part) % 7]);
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
    return 'every minute';
  }
  if (step(min) && hour === '*' && dom === '*' && dow === '*') {
    return `every ${step(min)} min`;
  }
  if (isNum(min) && step(hour) && dom === '*' && dow === '*') {
    return `every ${step(hour)}h at :${pad(min)}`;
  }
  if (isNum(min) && hour === '*' && dom === '*' && dow === '*') {
    return `hourly at :${pad(min)}`;
  }
  if (isNum(min) && isNum(hour)) {
    const t = `${pad(hour)}:${pad(min)}`;
    if (dom === '*' && dow === '*') return `daily at ${t}`;
    if (dom === '*') {
      const days = dayLabel(dow);
      return days ? `${days} at ${t}` : null;
    }
    if (isNum(dom) && dow === '*') return `monthly on day ${dom} at ${t}`;
  }
  return null;
}

function describeTrigger(s) {
  if (s.trigger_kind === 'every') return humanizeEvery(s.trigger_expr);
  if (s.trigger_kind === 'cron') {
    return humanizeCron(s.trigger_expr) || `cron ${s.trigger_expr}`;
  }
  if (s.trigger_kind === 'at') return `at ${formatDateTime(Number(s.trigger_expr))}`;
  return `${s.trigger_kind} ${s.trigger_expr}`;
}

// One-line format reminder per trigger kind, shown under the form.
const TRIGGER_HINTS = {
  every: 'Interval in seconds — 3600 runs hourly.',
  cron: 'min hour day month weekday — e.g. "0 9 * * 1-5" = weekdays at 09:00.',
  at: 'Runs once at the chosen local time.',
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
    if (!input.value) throw new Error('pick a date and time');
    const epoch = Math.floor(new Date(input.value).getTime() / 1000);
    if (!Number.isFinite(epoch)) throw new Error('invalid date');
    return String(epoch);
  }
  const v = input.value.trim();
  if (!v) throw new Error('enter a trigger expression');
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
  meta.textContent = s.active
    ? `${describeTrigger(s)} · next ${formatDateTime(s.next_fire)}`
    : `${describeTrigger(s)} · ${done ? 'done' : 'paused'}`;
  // The humanized trigger keeps the raw expression one hover away.
  meta.title = `${s.trigger_kind} ${s.trigger_expr}`;
  // Outcome of the most recent fire (None until it first completes).
  if (s.last_status) {
    const status = document.createElement('span');
    status.className = `sched-status ${s.last_status}`;
    status.textContent = s.last_status === 'ok' ? '✓' : '✕ failed';
    if (s.last_error) status.title = s.last_error;
    meta.append(' · ', status);
  }
  // Answer "where did my scheduled run go?" — jump to the last fire's chat.
  if (s.last_session_id) {
    const link = document.createElement('button');
    link.type = 'button';
    link.className = 'sched-last-run';
    link.textContent = 'last run ↗';
    link.title = 'Open the chat of the most recent run';
    link.addEventListener('click', () => onOpenSession(s.last_session_id));
    meta.append(' · ', link);
  }
  main.append(prompt, meta);

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

  btn('Run now', 'zap', async () => {
    try {
      await api.runSchedule(s.id);
      toast('Fired — running in the background');
    } catch (err) {
      toast(err.message || 'Couldn’t run schedule', { type: 'error' });
    } finally {
      onChange();
    }
  });

  // A finished one-shot can't meaningfully resume — no pause/resume for it.
  if (!done) {
    btn(s.active ? 'Pause' : 'Resume', s.active ? 'pause' : 'play', async () => {
      try {
        await api.setScheduleActive(s.id, !s.active);
        onChange();
      } catch (err) {
        toast(err.message || 'Couldn’t update schedule', { type: 'error' });
      }
    });
  }

  btn('Edit', 'pencil', () => onEdit(s));

  btn('Delete', 'x', async () => {
    if (!(await confirmDialog('Delete this schedule?'))) return;
    try {
      await api.deleteSchedule(s.id);
    } catch (err) {
      toast(err.message || 'Couldn’t delete schedule', { type: 'error' });
    } finally {
      onChange(); // refresh either way — a 404 just means it's already gone
    }
  });

  item.append(main, actions);
  return item;
}

export async function openSchedulesDialog() {
  const panel = document.createElement('div');
  panel.className = 'schedules-panel';
  panel.innerHTML = `
    <div class="schedules-head">
      <h3>Scheduled runs</h3>
      <button type="button" class="btn-icon sched-close" aria-label="Close">${icon('x', { size: 16 })}</button>
    </div>
    <form class="sched-form">
      <textarea class="dialog-input sched-input" rows="2" placeholder="Prompt to run on schedule…" required></textarea>
      <div class="sched-row">
        <select class="dialog-input sched-agent" aria-label="Agent" hidden></select>
        <select class="dialog-input sched-kind" aria-label="Trigger kind">
          <option value="every">Every</option>
          <option value="cron">Cron</option>
          <option value="at">At</option>
        </select>
        <span class="sched-expr-wrap"></span>
        <button type="button" class="btn btn-ghost btn-sm sched-cancel-edit" hidden>Cancel</button>
        <button type="submit" class="btn btn-primary btn-sm">Add</button>
      </div>
      <div class="sched-hint"></div>
    </form>
    <div class="sched-list"></div>`;

  const form = panel.querySelector('.sched-form');
  const input = panel.querySelector('.sched-input');
  const agentSel = panel.querySelector('.sched-agent');
  const kindSel = panel.querySelector('.sched-kind');
  const exprWrap = panel.querySelector('.sched-expr-wrap');
  const hintEl = panel.querySelector('.sched-hint');
  const submitBtn = panel.querySelector('.sched-form [type="submit"]');
  const cancelEditBtn = panel.querySelector('.sched-cancel-edit');
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
    submitBtn.textContent = 'Add';
    cancelEditBtn.hidden = true;
  }

  // Prefill the form from an existing row; submit then PATCHes it in place.
  function enterEditMode(s) {
    editingId = s.id;
    input.value = s.input;
    if (store.agents.length > 1 && s.agent) agentSel.value = s.agent;
    kindSel.value = s.trigger_kind;
    exprInput = attachExpr(buildExprInput(s.trigger_kind));
    exprInput.value =
      s.trigger_kind === 'at' ? epochToLocalInput(s.trigger_expr) : s.trigger_expr;
    exprWrap.replaceChildren(exprInput);
    syncHint();
    submitBtn.textContent = 'Save';
    cancelEditBtn.hidden = false;
    input.focus();
  }

  cancelEditBtn.addEventListener('click', exitEditMode);

  async function refresh() {
    try {
      const rows = await api.listSchedules();
      if (!rows.length) {
        listEl.innerHTML = '<div class="sched-empty">No schedules yet.</div>';
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
      listEl.innerHTML = '<div class="sched-empty">Couldn’t load schedules.</div>';
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
    try {
      if (editingId) {
        await api.updateSchedule(editingId, body);
        exitEditMode();
      } else {
        await api.createSchedule(body);
        input.value = '';
      }
      await refresh();
    } catch (err) {
      toast(err.message || 'Couldn’t save schedule', { type: 'error' });
    }
  });

  const dialog = showDialog({ body: panel });
  dialog.classList.add('dialog-wide');
  panel.querySelector('.sched-close').addEventListener('click', () => dialog.close());
  refresh();
}

export function initSchedules() {
  document
    .getElementById('schedules-btn')
    ?.addEventListener('click', openSchedulesDialog);
}
