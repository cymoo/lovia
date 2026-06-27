// Scheduled runs: a lightweight modal to list / create / pause / delete the
// cron · interval · one-shot schedules served by /api/schedules. Opened from the
// clock button in the topbar (shown only when the server advertises the
// `scheduling` feature).
import { api } from './api.js';
import { store } from './store.js';
import { showDialog, confirmDialog } from './ui.js';
import { toast } from './toast.js';

// ---- formatting ----------------------------------------------------------
function fmtTime(ts) {
  if (!ts) return '';
  const ms = ts > 1e12 ? ts : ts * 1000; // accept seconds or millis
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return String(ts);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

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
  if (s.trigger_kind === 'at') return `at ${fmtTime(Number(s.trigger_expr))}`;
  return `${s.trigger_kind} ${s.trigger_expr}`;
}

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

// ---- the dialog ----------------------------------------------------------
function rowEl(s, onChange) {
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
    ? `${describeTrigger(s)} · next ${fmtTime(s.next_fire)}`
    : `${describeTrigger(s)} · paused`;
  main.append(prompt, meta);

  const actions = document.createElement('div');
  actions.className = 'sched-item-actions';

  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.title = s.active ? 'Pause' : 'Resume';
  toggle.textContent = s.active ? '⏸' : '▶';
  toggle.addEventListener('click', async () => {
    try {
      await api.setScheduleActive(s.id, !s.active);
      onChange();
    } catch (err) {
      toast(err.message || 'Couldn’t update schedule', { type: 'error' });
    }
  });

  const del = document.createElement('button');
  del.type = 'button';
  del.title = 'Delete';
  del.textContent = '✕';
  del.addEventListener('click', async () => {
    if (!(await confirmDialog('Delete this schedule?'))) return;
    try {
      await api.deleteSchedule(s.id);
    } catch (err) {
      toast(err.message || 'Couldn’t delete schedule', { type: 'error' });
    } finally {
      onChange(); // refresh either way — a 404 just means it's already gone
    }
  });

  actions.append(toggle, del);
  item.append(main, actions);
  return item;
}

export async function openSchedulesDialog() {
  const panel = document.createElement('div');
  panel.className = 'schedules-panel';
  panel.innerHTML = `
    <div class="schedules-head">
      <h3>Scheduled runs</h3>
      <button type="button" class="btn-icon sched-close" aria-label="Close">✕</button>
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
        <button type="submit" class="btn btn-primary btn-sm">Add</button>
      </div>
    </form>
    <div class="sched-list"></div>`;

  const form = panel.querySelector('.sched-form');
  const input = panel.querySelector('.sched-input');
  const agentSel = panel.querySelector('.sched-agent');
  const kindSel = panel.querySelector('.sched-kind');
  const exprWrap = panel.querySelector('.sched-expr-wrap');
  const listEl = panel.querySelector('.sched-list');

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
  kindSel.addEventListener('change', () => {
    exprInput = buildExprInput(kindSel.value);
    exprWrap.replaceChildren(exprInput);
  });

  async function refresh() {
    try {
      const rows = await api.listSchedules();
      if (!rows.length) {
        listEl.innerHTML = '<div class="sched-empty">No schedules yet.</div>';
        return;
      }
      listEl.replaceChildren(...rows.map((s) => rowEl(s, refresh)));
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
      await api.createSchedule(body);
      input.value = '';
      await refresh();
    } catch (err) {
      toast(err.message || 'Couldn’t create schedule', { type: 'error' });
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
