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

function humanizeEvery(expr) {
  const secs = Number(expr);
  if (!Number.isFinite(secs)) return `every ${expr}`;
  if (secs % 3600 === 0) return `every ${secs / 3600}h`;
  if (secs % 60 === 0) return `every ${secs / 60}m`;
  return `every ${secs}s`;
}

function describeTrigger(s) {
  if (s.trigger_kind === 'every') return humanizeEvery(s.trigger_expr);
  if (s.trigger_kind === 'cron') return `cron ${s.trigger_expr}`;
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

function rowEl(s, { onChange, onEdit }) {
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

  let exprInput = buildExprInput(kindSel.value);
  exprWrap.appendChild(exprInput);
  const syncHint = () => { hintEl.textContent = TRIGGER_HINTS[kindSel.value] || ''; };
  syncHint();
  kindSel.addEventListener('change', () => {
    exprInput = buildExprInput(kindSel.value);
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
    exprInput = buildExprInput(s.trigger_kind);
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
        ...rows.map((s) => rowEl(s, { onChange: refresh, onEdit: enterEditMode })),
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
