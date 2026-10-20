// Tool views — a deployer's say in how a tool's card reads, registered with
// `window.lovia.registerToolView(name | '*', view)`. A pure registry (no DOM):
// tools.js applies the views to cards, export.js asks them for snapshots.

/**
 * What a view sees of one call: a frozen copy, rebuilt on every change.
 * @typedef {object} ToolInfo
 * @property {string} id
 * @property {string} name
 * @property {unknown} args The parsed arguments; undefined when unparsable or
 *   too large to keep (over 256 KB).
 * @property {'running' | 'awaiting' | 'done' | 'error'} status
 * @property {string | undefined} result Undefined until the call settles.
 * @property {boolean} isError
 * @property {any[] | undefined} images
 */
/**
 * @typedef {object} ToolView
 * @property {(tool: ToolInfo) => string | null | undefined} [summary] One line
 *   in place of the card's `name(args)`; null or undefined keeps the default.
 * @property {(el: HTMLElement, tool: ToolInfo,
 *   ctx: import('./components.js').RenderContext) =>
 *   void | (() => void) | Promise<void | (() => void)>} [render] Draw the
 *   card's body; called again on every change, after the previous call's
 *   cleanup. May return a cleanup function (or a promise of one).
 * @property {(tool: ToolInfo,
 *   ctx: import('./components.js').SnapshotContext) => string | Promise<string>} [snapshot]
 *   Static HTML for the chat's HTML export.
 */

/** The name a fallback view registers under: every tool without its own. */
export const ANY_TOOL = '*';
/** The component kind tools.js draws a view's `render` / `snapshot` under. */
export const TOOL_VIEW_KIND = 'lovia.tool-view';
const FIELDS = ['summary', 'render', 'snapshot'];

/** @type {Map<string, ToolView>} */
const views = new Map();
/**
 * lovia's own views of its built-in tools (builtin-views.js), below every
 * registered one — the '*' fallback included — plus the short label an
 * activity group's digest uses.
 * @type {Map<string, ToolView & { label?: () => string }>}
 */
const defaults = new Map();
/** @type {Set<(name: string) => void>} */
const watchers = new Set();

/**
 * Register (or replace) the view for tool `name`, or for every tool without
 * one of its own (`'*'`). Cards already on screen take it up at once.
 * @param {string} name
 * @param {ToolView} view
 */
export function registerToolView(name, view) {
  if (typeof name !== 'string' || !name) {
    throw new TypeError('lovia.registerToolView: expected a tool name or "*"');
  }
  const given = FIELDS.filter((f) => view?.[f] != null);
  if (!given.length || given.some((f) => typeof view[f] !== 'function')) {
    throw new TypeError(
      'lovia.registerToolView: expected { summary?, render?, snapshot? } functions',
    );
  }
  views.set(name, Object.freeze({ summary: view.summary, render: view.render, snapshot: view.snapshot }));
  for (const fn of watchers) fn(name);
}

/**
 * The view function `field` for tool `name`: its own view's, else the
 * fallback's — each field falls back on its own.
 * @template {'summary' | 'render' | 'snapshot'} F
 * @param {string} name
 * @param {F} field
 * @returns {ToolView[F] | undefined}
 */
export function toolViewField(name, field) {
  return toolViewChain(name, field)[0];
}

/**
 * Every view function `field` for tool `name`, most specific first: its own
 * view's, the fallback's, lovia's default. A summary that returns nothing
 * hands over to the next.
 * @template {'summary' | 'render' | 'snapshot'} F
 * @param {string} name
 * @param {F} field
 * @returns {NonNullable<ToolView[F]>[]}
 */
export function toolViewChain(name, field) {
  return [views.get(name), views.get(ANY_TOOL), defaults.get(name)]
    .map((view) => view?.[field])
    .filter((fn) => fn != null);
}

/**
 * Set lovia's default view of a built-in tool (not part of window.lovia).
 * @param {string} name
 * @param {ToolView & { label?: () => string }} view
 */
export function setDefaultView(name, view) {
  defaults.set(name, view);
}

/**
 * A tool's short display name: its default label, else the name itself.
 * @param {string} name
 */
export function toolLabel(name) {
  return defaults.get(name)?.label?.() ?? name;
}

/**
 * Call `fn` with the tool name whenever a view is registered.
 * @param {(name: string) => void} fn
 * @returns {() => void} Stops watching.
 */
export function watchToolViews(fn) {
  watchers.add(fn);
  return () => watchers.delete(fn);
}

/**
 * The frozen view of a call that a tool view gets.
 * @param {{ id: string, name: string, arguments?: string, status: ToolInfo['status'],
 *   result?: string, isError: boolean, images?: any[] }} state
 * @returns {Readonly<ToolInfo>}
 */
export function toolInfo(state) {
  let args;
  if (state.arguments) {
    try {
      args = JSON.parse(state.arguments);
    } catch { /* unparsable: undefined */ }
  }
  return Object.freeze({
    id: state.id,
    name: state.name,
    args,
    status: state.status,
    result: state.result,
    isError: state.isError,
    images: state.images,
  });
}
