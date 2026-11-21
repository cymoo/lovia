// The agent's plan (the Todo plugin's `todo_write` list), shown two ways:
//
// - The plan line above the composer: the current step and a count, opening to
//   the whole list. It shows while the chat's latest plan has work left (muted
//   when no run is going), and while a run finishes one; a finished plan goes
//   once the run ends. It never sits over the conversation.
// - A row in the reply for each update, folded with the other steps: its line
//   says what changed; opened, it shows the whole list as of that update.
//
// Rows diff against the plan before them, so chat.js notes each successful
// update here in transcript order (live and replay alike). Noting is also
// what makes a call a plan row: by its outcome, never by its tool's name.

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
 * What one update changed. Items have no ids (each update replaces the whole
 * list), so they match by text — in order, one for one, as two items may
 * share a text.
 * @param {PlanItem[] | null} prev
 * @param {PlanItem[]} next
 * @returns {{ done: PlanItem[], started: PlanItem[], added: number, removed: number,
 *   changed: Set<number> }} `changed`: indexes into `next` of new items and
 *   items whose status moved; empty for a first plan.
 */
export function planDiff(prev, next) {
  /** @type {Map<string, string[]>} each text's statuses, still unmatched */
  const was = new Map();
  for (const item of prev ?? []) {
    const queue = was.get(item.content);
    if (queue) queue.push(item.status);
    else was.set(item.content, [item.status]);
  }
  const out = { done: [], started: [], added: 0, removed: 0, changed: new Set() };
  next.forEach((item, i) => {
    const before = was.get(item.content)?.shift();
    if (before === undefined) out.added += 1;
    if (before === item.status) return;
    if (prev) out.changed.add(i);
    if (item.status === 'completed' && before !== undefined) out.done.push(item);
    if (item.status === 'in_progress') out.started.push(item);
  });
  for (const queue of was.values()) out.removed += queue.length;
  if (!prev) out.added = 0; // a first plan adds nothing: it is the plan
  return /** @type {any} */ (out);
}

/**
 * The line of an update's row: progress, then what changed.
 * @param {PlanItem[] | null} prev The plan before it (null: there was none).
 * @param {PlanItem[]} next
 * @returns {string}
 */
export function planSummary(prev, next) {
  if (!next.length) return t('plan.cleared');
  const done = next.filter((item) => item.status === 'completed').length;
  const count = `${done}/${next.length}`;
  if (!prev?.length) return `${count} · ${t('plan.created')}`;
  const d = planDiff(prev, next);
  const parts = [];
  if (d.done.length === 1) parts.push(t('plan.doneOne', { text: d.done[0].content }));
  else if (d.done.length > 1) parts.push(t('plan.doneMany', { n: d.done.length }));
  if (d.started.length) parts.push(t('plan.now', { text: planLabel(d.started[0]) }));
  if (d.added) parts.push(t('plan.added', { n: d.added }));
  if (d.removed) parts.push(t('plan.removed', { n: d.removed }));
  if (!parts.length) parts.push(t('plan.revised'));
  return [count, ...parts].join(' · ');
}

/**
 * The step the plan line names: the one in progress, else the next pending
 * one, else none (all done).
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

// ---- The chat's plan history: each update's predecessor --------------------

// Keyed by tool card (tools.js toolKey), not call id: a provider may reuse ids.
/** @type {Map<number, PlanItem[] | null>} */
const before = new Map();
/** @type {PlanItem[] | null} */
let latest = null;

/**
 * Record a successful update as it lands. A failed one never changed the
 * plan, so it is never noted. Without a card (none drawn), only the plan.
 * @param {number | undefined} key The update's tool card.
 * @param {PlanItem[]} items
 */
export function notePlan(key, items) {
  if (key !== undefined && !before.has(key)) before.set(key, latest);
  latest = items;
}

/**
 * Record a replayed update's card with the plan it replaced (the replay
 * worked that out over the whole history; the current plan is resetPlans').
 * @param {number | undefined} key
 * @param {PlanItem[] | null} prev
 */
export function placePlan(key, prev) {
  if (key !== undefined) before.set(key, prev);
}

/**
 * The plan the update on card `key` replaced; null for a first plan.
 * @param {number | undefined} key
 */
export function planBefore(key) {
  return (key !== undefined && before.get(key)) || null;
}

/**
 * Forget the chat's plan rows (another chat opened, or the chat redrawn).
 * @param {PlanItem[] | null} [current] The plan as of the redraw.
 */
export function resetPlans(current = null) {
  before.clear();
  latest = current;
}

// ---- The row view ----------------------------------------------------------

/** @type {import('./tool-views.js').ToolView['summary']} */
function rowSummary(tool) {
  // Only a noted update: another call of the same tool keeps its own line.
  if (!before.has(tool.key)) return null;
  const items = parsePlan(tool.args);
  return items && planSummary(planBefore(tool.key), items);
}

/** @type {Set<string>} tools whose default view already reads plan updates */
const layered = new Set();

/**
 * Let calls of tool `name` read as plan updates — the ones noted as such
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
  in_progress: () => icon('loader-circle', { size: 13 }),
  pending: () => '', // its ring is drawn in CSS
};

/**
 * The list, each item with its status mark; `changed` items are marked too.
 * `idle`: no run is at it, so the step in progress reads as its task, as the
 * plan line says it.
 * @param {PlanItem[]} items
 * @param {Set<number>} [changed]
 * @param {{ idle?: boolean }} [opts]
 */
export function planList(items, changed = new Set(), { idle = false } = {}) {
  const ul = document.createElement('ul');
  ul.className = 'lv-plan-list';
  items.forEach((item, i) => {
    // Idle, the step in progress waits like the line says: the ring, not a
    // spinner nobody turns — still the current one (lv-current).
    const waiting = idle && item.status === 'in_progress';
    const status = waiting ? 'pending' : item.status;
    const li = document.createElement('li');
    li.className = `lv-plan-item lv-${status}`;
    if (waiting) li.classList.add('lv-current');
    if (changed.has(i)) li.classList.add('lv-changed');
    const mark = document.createElement('span');
    mark.className = 'lv-plan-mark';
    mark.innerHTML = MARK[status]();
    const text = document.createElement('span');
    text.className = 'lv-plan-text';
    text.textContent = idle ? item.content : planLabel(item);
    li.append(mark, text);
    ul.append(li);
  });
  return ul;
}

/**
 * Draw a settled update's row body: the whole list as of that update, with
 * what it changed marked — and label the row "Plan" in its activity digest,
 * whatever its tool is called. Idempotent.
 * @param {HTMLElement} node The tool card.
 * @param {number | undefined} key Its toolKey.
 * @param {PlanItem[]} items
 */
export function fillPlanRow(node, key, items) {
  node.classList.add('lv-tool--plan');
  node.dataset.label = t('toolview.plan');
  relabelStep(node);
  const box = node.querySelector('.lv-tool-result-box');
  if (!box) return;
  box.querySelector(':scope > .lv-plan-list')?.remove();
  // A cleared plan has no list: its result text says so (styles.css shows
  // the text unless a list stands in for it).
  if (!items.length) return;
  const prev = planBefore(key);
  box.append(planList(items, prev ? planDiff(prev, items).changed : new Set()));
}

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
  body.replaceChildren(planList(items, new Set(), { idle: !live }));
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
