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
// update here in transcript order (live and replay alike).

import { t } from './i18n.js';
import { icon } from './icons.js';
import { setDefaultView } from './tool-views.js';

/** The Todo plugin's default tool name; a renamed one is learned at run time. */
export const PLAN_TOOL = 'todo_write';

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
  return todos.map((todo) => ({
    content: typeof todo?.content === 'string' ? todo.content : '',
    status: STATUSES.includes(todo?.status) ? todo.status : 'pending',
    active_form: typeof todo?.active_form === 'string' ? todo.active_form : null,
  }));
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
 * list), so they match by text.
 * @param {PlanItem[] | null} prev
 * @param {PlanItem[]} next
 * @returns {{ done: PlanItem[], started: PlanItem[], added: number, removed: number,
 *   changed: Set<number> }} `changed`: indexes into `next` of new items and
 *   items whose status moved; empty for a first plan.
 */
export function planDiff(prev, next) {
  const was = new Map((prev ?? []).map((item) => [item.content, item.status]));
  const now = new Set(next.map((item) => item.content));
  const out = { done: [], started: [], added: 0, removed: 0, changed: new Set() };
  next.forEach((item, i) => {
    const before = was.get(item.content);
    if (before === undefined) out.added += 1;
    if (before === item.status) return;
    if (prev) out.changed.add(i);
    if (item.status === 'completed' && before !== undefined) out.done.push(item);
    if (item.status === 'in_progress') out.started.push(item);
  });
  for (const content of was.keys()) if (!now.has(content)) out.removed += 1;
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
 * The list as the plugin's own result renders it (what a row's copy copies).
 * @param {PlanItem[]} items
 */
export function planText(items) {
  if (!items.length) return 'Todo list cleared.';
  const box = { pending: '[ ]', in_progress: '[~]', completed: '[x]' };
  return `Updated todo list:\n${items.map((i) => `${box[i.status]} ${planLabel(i)}`).join('\n')}`;
}

/**
 * Whether a result is the plugin's own rendering of a plan update — how a
 * replayed chat recognises a renamed plan tool. Mirrors `_render_result` in
 * lovia/plugins/todo.py.
 * @param {string | undefined} text
 */
export function isPlanResult(text) {
  return typeof text === 'string' && (text.startsWith('Updated todo list:') || text === 'Todo list cleared.');
}

// ---- The chat's plan history: each update's predecessor --------------------

/** @type {Map<string, PlanItem[] | null>} */
const before = new Map();
/** @type {PlanItem[] | null} */
let latest = null;

/**
 * Record a successful update, in transcript order. A failed one never changed
 * the plan, so it is never noted.
 * @param {string} callId
 * @param {PlanItem[]} items
 */
export function notePlan(callId, items) {
  if (!before.has(callId)) before.set(callId, latest);
  latest = items;
}

/**
 * The plan an update replaced: as noted, else (an update still running) the
 * latest one so far.
 * @param {string} callId
 */
export function planBefore(callId) {
  return before.has(callId) ? before.get(callId) ?? null : latest;
}

/** Forget the chat's plans (another chat opened, or the chat redrawn). */
export function resetPlans() {
  before.clear();
  latest = null;
}

// ---- The row view ----------------------------------------------------------

/** @type {import('./tool-views.js').ToolView['summary']} */
function rowSummary(tool) {
  if (tool.isError) return t('plan.failed');
  const items = parsePlan(tool.args);
  return items && planSummary(planBefore(tool.id), items);
}

/**
 * Read calls of tool `name` as plan updates (the default name is registered
 * below; chat.js adds a renamed one when it meets it).
 * @param {string} name
 */
export function usePlanView(name) {
  setDefaultView(name, { summary: rowSummary, label: () => t('toolview.plan') });
}
usePlanView(PLAN_TOOL);

// ---- DOM -------------------------------------------------------------------

const MARK = {
  completed: () => icon('check', { size: 13 }),
  in_progress: () => icon('loader-circle', { size: 13 }),
  pending: () => '', // its ring is drawn in CSS
};

/**
 * The list, each item with its status mark; `changed` items are marked too.
 * @param {PlanItem[]} items
 * @param {Set<number>} [changed]
 */
export function planList(items, changed = new Set()) {
  const ul = document.createElement('ul');
  ul.className = 'lv-plan-list';
  items.forEach((item, i) => {
    const li = document.createElement('li');
    li.className = `lv-plan-item lv-${item.status}`;
    if (changed.has(i)) li.classList.add('lv-changed');
    const mark = document.createElement('span');
    mark.className = 'lv-plan-mark';
    mark.innerHTML = MARK[item.status]();
    const text = document.createElement('span');
    text.className = 'lv-plan-text';
    text.textContent = planLabel(item);
    li.append(mark, text);
    ul.append(li);
  });
  return ul;
}

/**
 * Draw a settled update's row body: the whole list as of that update, with
 * what it changed marked. Idempotent.
 * @param {HTMLElement} node The tool card.
 * @param {string} callId
 * @param {PlanItem[]} items
 */
export function fillPlanRow(node, callId, items) {
  node.classList.add('lv-tool--plan');
  const box = node.querySelector('.lv-tool-result-box');
  if (!box) return;
  box.querySelector(':scope > .lv-plan-list')?.remove();
  // A cleared plan has no list: its result text says so (styles.css shows
  // the text unless a list stands in for it).
  if (!items.length) return;
  const prev = planBefore(callId);
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
 * @param {{ update?: boolean }} [opts] `update`: an update that just landed —
 *   false for a plan read back from history, which is no news.
 */
export function showPlan(items, { update = true } = {}) {
  plan.items = items;
  const step = currentStep(items);
  plan.finished = update && plan.live && items.length > 0 && !step;
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
  body.replaceChildren(planList(items));
  const now = /** @type {HTMLElement | null} */ (body.querySelector('.lv-in_progress'));
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
