// Activity groups — a reply's consecutive working steps (reasoning, tool calls)
// folded under one summary line, so the answer isn't buried beneath them.
//
// Grouping happens where content enters a bubble (chat.js
// appendBubbleContent), which both the live stream and history replay go
// through, so a reopened chat folds exactly as the live run did. Every step
// lands in a group, a lone one included, so a step looks the same however many
// follow it. Anything else — the reply's text, an approval or question card,
// an error — ends the run of steps and is never folded.
//
// A group is never opened or closed for the user: while its run works, the
// folded line names the latest step (styles.css swaps it for the digest), so
// progress shows without the layout jumping.

import { t } from './i18n.js';
import { icon } from './icons.js';
import { toolLabel } from './tool-views.js';

/**
 * Whether `node` is a working step that may fold into a group. A tool card
 * whose result is the payload (a UI component, images) is not: it stays in
 * view — see liftStep.
 * @param {Element} node
 */
export function isStep(node) {
  const c = node.classList;
  if (c.contains('lv-reasoning')) return true;
  return c.contains('lv-tool') && !c.contains('lv-tool--ui') && !node.querySelector('.lv-tool-result-images');
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
const stepsOf = (group) => /** @type {HTMLElement} */ (group.querySelector(':scope > .lv-activity-steps'));

/** @param {Element | null} node @returns {HTMLDetailsElement | null} */
function groupOf(node) {
  const group = node?.parentElement?.parentElement;
  return group?.classList.contains('lv-activity') ? /** @type {HTMLDetailsElement} */ (group) : null;
}

/** @param {boolean} [open] */
function makeGroup(open = false) {
  const group = document.createElement('details');
  group.className = 'lv-activity';
  group.dataset.part = 'activity';
  group.open = open;
  const summary = document.createElement('summary');
  summary.innerHTML =
    // Shown only when a step failed (styles.css); otherwise the line is text.
    `<span class="lv-activity-icon">${icon('triangle-alert', { size: 14 })}</span>` +
    '<span class="lv-activity-label"></span>' +
    // The latest step, shown in the label's place while the run works.
    '<span class="lv-activity-now"></span>';
  const steps = document.createElement('div');
  steps.className = 'lv-activity-steps';
  group.append(summary, steps);
  return group;
}

/**
 * A step's own line: a tool card's summary (its view's, else its label), or
 * "Thinking…".
 * @param {Element} node
 */
function stepLine(node) {
  const tool = /** @type {HTMLElement} */ (node).dataset.tool;
  if (!tool) return t('chat.thinking');
  return node.querySelector(':scope > summary > .lv-tool-summary')?.textContent || toolLabel(tool);
}

/** @param {HTMLElement} group */
function relabel(group) {
  const kids = [...stepsOf(group).children];
  const steps = kids.map((n) => {
    const tool = /** @type {HTMLElement} */ (n).dataset.tool;
    return { reasoning: n.classList.contains('lv-reasoning'), tool: tool && toolLabel(tool) };
  });
  const label = group.querySelector('.lv-activity-label');
  // One tool call reads as itself, not as a digest of one.
  if (label) label.textContent = steps.length === 1 && steps[0].tool ? stepLine(kids[0]) : summarize(steps).join(' · ');
  const now = group.querySelector('.lv-activity-now');
  if (now && kids.length) now.textContent = stepLine(kids[kids.length - 1]);
}

// Relabel a group, or remove it once its last step is gone.
/** @param {HTMLElement} group */
function normalize(group) {
  if (stepsOf(group).children.length) relabel(group);
  else group.remove();
}

/**
 * Put a step into `bubble` before `before` (its footer, or null for the end),
 * folding it into the step run that ends there, or starting one.
 * @param {HTMLElement} bubble
 * @param {HTMLElement} node
 * @param {Element | null} before
 */
export function placeStep(bubble, node, before) {
  const prev = before ? before.previousElementSibling : bubble.lastElementChild;
  let group = /** @type {HTMLElement | null} */ (prev?.classList.contains('lv-activity') ? prev : null);
  if (!group) {
    group = makeGroup();
    bubble.insertBefore(group, before);
  }
  stepsOf(group).append(node);
  relabel(group);
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
  if (rest.length) {
    const tail = makeGroup(group.open);
    stepsOf(tail).append(...rest);
    node.after(tail);
    relabel(tail);
  }
  normalize(group);
}

/**
 * Remove a step, and its group with it when it was the last.
 * @param {HTMLElement | null} node
 */
export function removeStep(node) {
  if (!node) return;
  const group = groupOf(node);
  node.remove();
  if (group) normalize(group);
}
