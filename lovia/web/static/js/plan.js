// The agent's plan (the Todo plugin's `todo_write` list), shown two ways:
//
// - The plan line above the composer: the current step and a count, opening to
//   the whole list. It shows while the chat's latest plan has work left (muted
//   when no run is going), and while a run finishes one; a finished plan goes
//   once the run ends. It never sits over the conversation.
// - A row in the reply for each update, folded with the other steps: its line
//   says where the plan stands after it; opened, it shows the whole list.
//
// Rows say the plan's state, not what changed since the last one: the list
// has no ids, so a diff could only guess which item became which (models
// reword, reorder, and write statuses into the text), and a guess misleads.
//
// chat.js marks each plan update's card here (live and replay alike): by its
// outcome, never by its tool's name.

import { relabelStep } from './activity.js';
import { t } from './i18n.js';
import { icon } from './icons.js';
import { defaultView, setDefaultView } from './tool-views.js';

/**
 * @typedef {{ content: string, status: 'pending' | 'in_progress' | 'completed',
 *   active_form: string | null }} PlanItem
 */

const STATUSES = ['pending', 'in_progress', 'completed'];

/**
 * A plan update's items, from its call arguments (a JSON string or the parsed
 * object), or null when they aren't a todo list.
 * @param {unknown} args
 * @returns {PlanItem[] | null}
 */
export function parsePlan(args) {
  let obj = args;
  if (typeof args === 'string') {
    try {
      obj = JSON.parse(args);
    } catch {
      return null;
    }
  }
  const todos = /** @type {any} */ (obj)?.todos;
  if (!Array.isArray(todos)) return null;
  let active = false;
  return todos.map((todo) => {
    let status = STATUSES.includes(todo?.status) ? todo.status : 'pending';
    // The plugin keeps one item in progress and demotes the rest
    // (`_normalize` in lovia/plugins/todo.py): read arguments as it stored them.
    if (status === 'in_progress') {
      if (active) status = 'pending';
      active = true;
    }
    return {
      content: typeof todo?.content === 'string' ? todo.content : '',
      status,
      active_form: typeof todo?.active_form === 'string' ? todo.active_form : null,
    };
  });
}

/**
 * How an item reads: its present-tense form while in progress.
 * @param {PlanItem} item
 */
export function planLabel(item) {
  return item.status === 'in_progress' && item.active_form ? item.active_form : item.content;
}

/**
 * The step the plan names: the one in progress, else the next pending one,
 * else none (all done).
 * @param {PlanItem[]} items
 * @returns {{ item: PlanItem, next: boolean } | null}
 */
export function currentStep(items) {
  const now = items.find((item) => item.status === 'in_progress');
  if (now) return { item: now, next: false };
  const next = items.find((item) => item.status === 'pending');
  return next ? { item: next, next: true } : null;
}

/**
 * The line of an update's row: the plan as it stands after it — progress and
 * the step at hand ("Plan 1/3 · Now “Running task two”").
 * @param {PlanItem[]} items
 * @returns {string}
 */
export function planSummary(items) {
  if (!items.length) return t('plan.cleared');
  const done = items.filter((item) => item.status === 'completed').length;
  const head = `${t('plan.label')} ${done}/${items.length}`;
  const step = currentStep(items);
  const where = !step
    ? t('plan.rowDone')
    : step.next
      ? t('plan.next', { text: step.item.content })
      : t('plan.now', { text: planLabel(step.item) });
  return `${head} · ${where}`;
}

/**
 * The list as the plugin's own result renders it, byte for byte — mirrors
 * `_render_result` / `render_todos` in lovia/plugins/todo.py. What a row's
 * copy copies, and how a replayed call is known for a plan update: its result
 * is exactly this rendering of its own arguments.
 * @param {PlanItem[]} items
 */
export function planText(items) {
  if (!items.length) return 'Todo list cleared.';
  const box = { pending: '[ ]', in_progress: '[~]', completed: '[x]' };
  return `Updated todo list:\n${items.map((i) => `${box[i.status]} ${planLabel(i)}`).join('\n')}`;
}

// The marker `truncate_tool_output` (lovia/tools/base.py) puts where it cut a
// long result, naming the full length.
const TRUNCATED = /\n\n\[\.\.\. tool output truncated: kept [\d,]+ of ([\d,]+) chars \.\.\.\]\n\n/;

/**
 * Whether a stored result is the plugin's rendering of `items` — whole, or as
 * the agent's `max_tool_output_chars` cut it (head, marker, tail): what a
 * replay knows a plan update by.
 * @param {string} text
 * @param {PlanItem[]} items
 */
export function isPlanOutput(text, items) {
  const full = planText(items);
  if (text === full) return true;
  const cut = TRUNCATED.exec(text);
  // Python counts code points, not UTF-16 units.
  if (!cut || Number(cut[1].replace(/,/g, '')) !== [...full].length) return false;
  return full.startsWith(text.slice(0, cut.index)) && full.endsWith(text.slice(cut.index + cut[0].length));
}

// ---- Plan rows ---------------------------------------------------------------

// The chat's plan-update cards, by tool card (tools.js toolKey), not call id:
// a provider may reuse ids, and another call of the same tool is no update.
/** @type {Set<number>} */
const rows = new Set();

/**
 * Mark the card `key` as a successful plan update — before it settles, so its
 * line reads as one at once. A failed update never changed the plan.
 * @param {number | undefined} key
 */
export function markPlanRow(key) {
  if (key !== undefined) rows.add(key);
}

/** Forget the chat's plan rows (another chat opened, or the chat redrawn). */
export function resetPlans() {
  rows.clear();
}

/** @type {import('./tool-views.js').ToolView['summary']} */
function rowSummary(tool) {
  if (!rows.has(tool.key)) return null;
  const items = parsePlan(tool.args);
  return items && planSummary(items);
}

/** @type {Set<string>} tools whose default view already reads plan updates */
const layered = new Set();

/**
 * Let calls of tool `name` read as plan updates — the ones marked as such
 * (chat.js calls it for each tool it has seen land one). Layered over the
 * tool's own default view, which every other call of it keeps: a plan tool
 * may share its name with a built-in one (`start_session` keeps its card).
 * The short label an activity digest uses is the card's own (fillPlanRow),
 * as a name says nothing about a single call.
 * @param {string} name
 */
export function usePlanView(name) {
  if (layered.has(name)) return;
  layered.add(name);
  const base = defaultView(name);
  setDefaultView(name, {
    ...base,
    summary: (tool) => rowSummary(tool) ?? base?.summary?.(tool) ?? null,
  });
}

// ---- DOM -------------------------------------------------------------------

const MARK = {
  completed: () => icon('check', { size: 13 }),
  in_progress: () => icon('loader-circle', { size: 13 }), // spun by CSS while live
  pending: () => '', // its ring is drawn in CSS
};

/**
 * The list, each item with its status mark. `mode` says what it shows:
 * - `live`: the plan a run is working through — the step in progress spins,
 *   as the plan line's does;
 * - `idle`: the plan with no run at it — that step waits like the line says
 *   (the ring, its task's words), still the current one (lv-current);
 * - `snapshot`: the plan as of one update in a reply — that step was the
 *   current one then, so a still dot, not a spinner frozen mid-turn.
 * @param {PlanItem[]} items
 * @param {'live' | 'idle' | 'snapshot'} [mode]
 */
export function planList(items, mode = 'snapshot') {
  const ul = document.createElement('ul');
  ul.className = `lv-plan-list lv-${mode}`;
  const idle = mode === 'idle';
  for (const item of items) {
    const current = item.status === 'in_progress';
    const status = idle && current ? 'pending' : item.status;
    const li = document.createElement('li');
    li.className = `lv-plan-item lv-${status}`;
    if (idle && current) li.classList.add('lv-current');
    const mark = document.createElement('span');
    mark.className = 'lv-plan-mark';
    mark.innerHTML =
      mode === 'snapshot' && current ? icon('circle-dot', { size: 13 }) : MARK[status]();
    const text = document.createElement('span');
    text.className = 'lv-plan-text';
    text.textContent = idle ? item.content : planLabel(item);
    li.append(mark, text);
    ul.append(li);
  }
  return ul;
}

/**
 * Draw a settled update's row body — the whole list as of that update — and
 * label the row "Plan" in its activity digest, whatever its tool is called.
 * Idempotent.
 * @param {HTMLElement} node The tool card.
 * @param {PlanItem[]} items
 */
export function fillPlanRow(node, items) {
  node.classList.add('lv-tool--plan');
  node.dataset.label = t('toolview.plan');
  relabelStep(node);
  const box = node.querySelector('.lv-tool-result-box');
  if (!box) return;
  box.querySelector(':scope > .lv-plan-list')?.remove();
  // A cleared plan has no list: its result text says so (styles.css shows
  // the text unless a list stands in for it).
  if (items.length) box.append(planList(items, 'snapshot'));
}

// ---- The plan line -----------------------------------------------------------

const OPEN_KEY = 'lovia.plan.open';
const plan = {
  items: /** @type {PlanItem[]} */ ([]),
  live: false,
  open: readOpen(),
  // The run going now finished the plan: its line stays until the run ends.
  finished: false,
  // The step last announced, so a run starting or ending doesn't re-announce it.
  spoken: '',
};

function readOpen() {
  try {
    return globalThis.localStorage?.getItem(OPEN_KEY) === '1';
  } catch {
    return false;
  }
}

/**
 * Show `items` as the chat's current plan (an empty list hides the line).
 * @param {PlanItem[]} items
 * @param {{ update?: boolean, lastTurn?: boolean }} [opts] `update`: an update
 *   that just landed — false for a plan read back from history, which is no
 *   news. `lastTurn`: read back, it came after the last user message, so a run
 *   still going (a reconnect) is the one that wrote it.
 */
export function showPlan(items, { update = true, lastTurn = false } = {}) {
  plan.items = items;
  const step = currentStep(items);
  plan.finished = (update || lastTurn) && plan.live && items.length > 0 && !step;
  // Say a new step out loud as a run works through the plan — not when a
  // chat opens on one (the line itself is no live region: a run starting or
  // ending rewords it without the plan changing).
  const spoken = !items.length ? '' : step ? planLabel(step.item) : t('plan.allDone');
  if (update && plan.live && spoken && spoken !== plan.spoken) announce(spoken);
  plan.spoken = spoken;
  drawPlan();
}

/**
 * Tell the plan line whether a run is going in this chat.
 * @param {boolean} live
 */
export function setPlanLive(live) {
  plan.live = live;
  if (live) plan.finished = false; // a plan finished earlier is no news now
  drawPlan();
}

/** A user message reached the running run (steered in): as at a run's start,
 *  a plan finished before it is no news — renderHistory reads it so too. */
export function planUserTurn() {
  plan.finished = false;
  drawPlan();
}

/** @param {string} text */
function announce(text) {
  const el = document.getElementById('sr-status'); // outside the busy transcript
  if (!el) return;
  el.textContent = '';
  setTimeout(() => {
    el.textContent = text;
  }, 30);
}

function drawPlan() {
  const root = document.getElementById('plan');
  const line = document.getElementById('plan-toggle');
  const body = document.getElementById('plan-list');
  if (!root || !line || !body) return;
  const { items, live } = plan;
  const step = currentStep(items);
  if (!items.length || (!step && !(live && plan.finished))) {
    root.hidden = true;
    body.replaceChildren();
    return;
  }
  root.hidden = false;
  root.classList.toggle('lv-idle', !live);
  root.classList.toggle('lv-done', !step);
  const done = items.filter((item) => item.status === 'completed').length;
  // A still spinner reads as stalled: with no run going, the step waits, so
  // it gets the waiting ring (the open list draws it the same).
  const status = !step ? 'completed' : live && !step.next ? 'in_progress' : 'pending';
  /** @type {HTMLElement} */ (line.querySelector('.lv-plan-mark')).innerHTML = MARK[status]();
  // The present-tense form only while a run is at it: an idle plan's step
  // reads as the task it is.
  /** @type {HTMLElement} */ (line.querySelector('.lv-plan-step')).textContent = !step
    ? t('plan.allDone')
    : step.next
      ? t('plan.next', { text: step.item.content })
      : live
        ? planLabel(step.item)
        : step.item.content;
  /** @type {HTMLElement} */ (line.querySelector('.lv-plan-count')).textContent = `${done}/${items.length}`;
  line.setAttribute('aria-expanded', String(plan.open));
  line.title = plan.open ? t('plan.hide') : t('plan.show');
  body.hidden = !plan.open;
  body.replaceChildren(planList(items, live ? 'live' : 'idle'));
  const now = /** @type {HTMLElement | null} */ (body.querySelector('.lv-in_progress, .lv-current'));
  if (plan.open && now) {
    // A long list scrolls itself (never the page) to the step in progress.
    body.scrollTop = now.offsetTop - (body.clientHeight - now.offsetHeight) / 2;
  }
}

/** Wire the plan line's toggle (once, at boot). */
export function initPlan() {
  document.getElementById('plan-toggle')?.addEventListener('click', () => {
    plan.open = !plan.open;
    try {
      globalThis.localStorage?.setItem(OPEN_KEY, plan.open ? '1' : '0');
    } catch { /* storage off: the choice lasts this page */ }
    // The list takes its height from the conversation above it: keep what
    // the reader sees at the bottom where it is, as the composer growing would.
    const transcript = document.getElementById('transcript');
    const below = transcript ? transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight : 0;
    drawPlan();
    if (transcript) transcript.scrollTop = transcript.scrollHeight - transcript.clientHeight - below;
  });
}
