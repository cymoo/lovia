// Activity groups — a reply's consecutive working steps (reasoning, tool calls)
// folded under one summary line, so the answer isn't buried beneath them.
//
// Grouping happens where content enters a bubble (chat.js
// appendBubbleContent), which both the live stream and history replay go
// through, so a reopened chat folds exactly as the live run did. A lone step
// stays as it is; a second consecutive one wraps both. Anything else — the
// reply's text, an approval or question card, an error — ends the run of steps
// and is never folded. A group stays open while its steps happen and closes
// once the reply's text starts (or the reply ends), unless the user toggled it
// or a step still waits on them.

import { t } from './i18n.js';
import { icon } from './icons.js';

/**
 * Whether `node` is a working step that may fold into a group. A tool card
 * whose result is the payload (a UI component, images) is not: it stays in
 * view — see liftStep.
 * @param {Element} node
 */
export function isStep(node) {
  const c = node.classList;
  if (c.contains('reasoning') || c.contains('approval-auto')) return true;
  return c.contains('tool') && !c.contains('tool--ui') && !node.querySelector('.tool-result-images');
}

/**
 * The one-line digest of a group's steps: "Thought", then each tool once, in
 * order, with a count when it ran more than once; past `max` tools, "+N".
 * @param {{ reasoning?: boolean, tool?: string }[]} steps
 * @param {number} [max]
 * @returns {string[]}
 */
export function summarize(steps, max = 4) {
  const counts = new Map();
  let thought = false;
  for (const s of steps) {
    if (s.reasoning) thought = true;
    else if (s.tool) counts.set(s.tool, (counts.get(s.tool) ?? 0) + 1);
  }
  const tools = [...counts].map(([name, n]) => (n > 1 ? `${name} ×${n}` : name));
  const parts = thought ? [t('activity.thought')] : [];
  parts.push(...tools.slice(0, max));
  if (tools.length > max) parts.push(`+${tools.length - max}`);
  return parts;
}

/** @param {HTMLElement} group */
const stepsOf = (group) => /** @type {HTMLElement} */ (group.querySelector(':scope > .activity-steps'));

/** @param {Element | null} node @returns {HTMLDetailsElement | null} */
function groupOf(node) {
  const group = node?.parentElement?.parentElement;
  return group?.classList.contains('activity') ? /** @type {HTMLDetailsElement} */ (group) : null;
}

/** @param {boolean} open */
function makeGroup(open) {
  const group = document.createElement('details');
  group.className = 'activity';
  group.dataset.part = 'activity';
  group.open = open;
  const summary = document.createElement('summary');
  summary.innerHTML =
    `<span class="activity-icon">${icon('layers', { size: 14 })}</span>` +
    '<span class="activity-label"></span>';
  // A click or keypress on the summary is the user's say; programmatic
  // open/close (sealActivity) must not override it.
  summary.addEventListener('click', () => {
    group.dataset.userToggled = '';
  });
  const steps = document.createElement('div');
  steps.className = 'activity-steps';
  group.append(summary, steps);
  return group;
}

/** @param {HTMLElement} group */
function relabel(group) {
  const steps = [...stepsOf(group).children].map((n) => ({
    reasoning: n.classList.contains('reasoning'),
    tool: /** @type {HTMLElement} */ (n).dataset.tool,
  }));
  const label = group.querySelector('.activity-label');
  if (label) label.textContent = summarize(steps).join(' · ');
}

// Unwrap a group left with a single step (or remove an empty one).
/** @param {HTMLElement} group */
function normalize(group) {
  const kids = [...stepsOf(group).children];
  if (kids.length > 1) relabel(group);
  else group.replaceWith(...kids);
}

/**
 * Put a step into `bubble` before `before` (its footer, or null for the end),
 * folding it into the step run that ends there.
 * @param {HTMLElement} bubble
 * @param {HTMLElement} node
 * @param {Element | null} before
 */
export function placeStep(bubble, node, before) {
  const prev = before ? before.previousElementSibling : bubble.lastElementChild;
  if (prev?.classList.contains('activity')) {
    stepsOf(/** @type {HTMLElement} */ (prev)).append(node);
    relabel(/** @type {HTMLElement} */ (prev));
  } else if (prev && isStep(prev)) {
    const group = makeGroup(true);
    bubble.insertBefore(group, before);
    stepsOf(group).append(prev, node);
    relabel(group);
  } else {
    bubble.insertBefore(node, before);
  }
}

/**
 * Close the bubble's open groups: its reply has moved on. A group the user
 * toggled, or one whose step waits on them, keeps its state.
 * @param {HTMLElement | null} bubble
 */
export function sealActivity(bubble) {
  for (const group of bubble?.querySelectorAll(':scope > .activity[open]') ?? []) {
    const g = /** @type {HTMLDetailsElement} */ (group);
    if ('userToggled' in g.dataset || g.querySelector('[data-status="awaiting"]')) continue;
    g.open = false;
  }
}

/**
 * Move a step that turned out to be the payload out of its group, into view,
 * splitting the group around it. Call before a UI component mounts in it:
 * moving a mounted iframe would reload it.
 * @param {HTMLElement} node
 */
export function liftStep(node) {
  const group = groupOf(node);
  if (!group) return;
  const rest = [];
  for (let n = node.nextElementSibling; n; n = n.nextElementSibling) rest.push(n);
  group.after(node);
  if (rest.length > 1) {
    const tail = makeGroup(group.open);
    stepsOf(tail).append(...rest);
    node.after(tail);
    relabel(tail);
  } else if (rest.length) {
    node.after(rest[0]);
  }
  normalize(group);
}

/**
 * Remove a step, unwrapping its group if one step is left.
 * @param {HTMLElement | null} node
 */
export function removeStep(node) {
  if (!node) return;
  const group = groupOf(node);
  node.remove();
  if (group) normalize(group);
}
