// UI components: a tool result rendered as UI instead of text.
//
// A tool opts in by returning a UI envelope (lovia/web/components.py
// `ui_result`) — {"lovia_ui": {"kind", "props" | "from_args"}} — and the
// renderer registered for `kind` draws it into the tool card. Renderers come
// from the page itself or from `ChatUI(scripts=...)` modules calling
// `window.lovia.registerRenderer(kind, render)`. Design notes:
// docs/design/web-ui-components.md.
//
// No DOM or app-module access at import time: the host (chat.js) injects
// labels and the render context through `configureComponents`, which keeps
// this module unit-testable under node.

/** @typedef {{ kind: string, props: unknown }} UiComponent */
/** @typedef {() => void} Cleanup */
/**
 * @typedef {object} RenderContext
 * @property {string} kind
 * @property {'light' | 'dark'} theme
 * @property {'en' | 'zh'} lang
 * @property {string} basePath The mount prefix, for the deployer's own endpoints.
 * @property {string | null} sessionId
 * @property {string | null} callId
 * @property {(text: string) => boolean} send Post a user message; needs a user gesture.
 */
/**
 * @typedef {(el: HTMLElement, props: unknown, ctx: RenderContext) =>
 *   void | Cleanup | Promise<void | Cleanup>} Renderer
 */
/**
 * @typedef {object} Mount
 * @property {HTMLElement} el
 * @property {string} kind
 * @property {unknown} props
 * @property {string | null} callId
 * @property {Cleanup | null} cleanup
 * @property {number} gen Bumped per render/teardown, so a late async cleanup
 *   knows it was superseded.
 */

// Mirrors _KIND_RE in lovia/web/components.py.
const KIND_RE = /^[a-z][a-z0-9_.-]{0,63}$/;
// json.dumps output of a one-key {"lovia_ui": ...} dict always starts so:
// a cheap test before JSON.parse on every tool result.
const PREFIX = '{"lovia_ui"';

/**
 * The component a tool result carries, or null when it is ordinary output.
 * `args` is the call's raw arguments JSON, read by the `from_args` form.
 * @param {string} text Tool output.
 * @param {string | undefined} args
 * @returns {UiComponent | null}
 */
export function parseEnvelope(text, args) {
  const s = String(text ?? '').trimStart();
  if (!s.startsWith(PREFIX)) return null;
  let env;
  try {
    env = JSON.parse(s)?.lovia_ui;
  } catch {
    return null;
  }
  // typeof first: RegExp.test would coerce undefined/null/true to a string.
  if (!env || typeof env !== 'object' || typeof env.kind !== 'string' || !KIND_RE.test(env.kind)) {
    return null;
  }
  if ('props' in env) return { kind: env.kind, props: env.props };
  const ref = env.from_args;
  if (ref !== true && (typeof ref !== 'string' || !ref)) return null;
  let obj;
  try {
    obj = JSON.parse(args ?? '');
  } catch {
    return null;
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
  const props = ref === true ? obj : obj[ref];
  return props === undefined ? null : { kind: env.kind, props };
}

/** @type {Map<string, Renderer>} */
const renderers = new Map();
/** @type {Set<Mount>} */
const mounts = new Set();

const host = {
  /** @type {() => Omit<RenderContext, 'kind' | 'callId' | 'send'>} */
  context: () => ({ theme: 'light', lang: 'en', basePath: '', sessionId: null }),
  /** @type {(text: string) => boolean} */
  send: () => false,
  /** @type {(key: 'missing' | 'failed', kind: string) => string} */
  label: (key, kind) => (key === 'missing' ? `No renderer for “${kind}”` : 'Render failed'),
};

/**
 * Wire the registry to the page: render context, message sending, notice
 * labels. Also publishes `window.lovia.registerRenderer`.
 * @param {Partial<typeof host>} opts
 */
export function configureComponents(opts) {
  Object.assign(host, opts);
  if (typeof window !== 'undefined') {
    window.lovia = Object.assign(window.lovia || {}, { registerRenderer });
  }
}

/**
 * Register (or replace) the renderer for `kind`. Components of that kind
 * already on screen — waiting for it, or drawn by the one it replaces —
 * render again, so script load order never matters.
 * @param {string} kind
 * @param {Renderer} render
 */
export function registerRenderer(kind, render) {
  if (typeof kind !== 'string' || !KIND_RE.test(kind)) {
    throw new TypeError(`lovia.registerRenderer: invalid kind ${JSON.stringify(kind)}`);
  }
  if (typeof render !== 'function') {
    throw new TypeError('lovia.registerRenderer: render must be a function');
  }
  renderers.set(kind, render);
  for (const m of mounts) if (m.kind === kind) run(m);
}

/** @param {Cleanup | null} fn */
function safeCleanup(fn) {
  try {
    fn?.();
  } catch (err) {
    console.error('lovia component cleanup failed:', err);
  }
}

/** @param {Mount} m */
function teardown(m) {
  m.gen++;
  const fn = m.cleanup;
  m.cleanup = null;
  safeCleanup(fn);
  m.el.replaceChildren();
}

/**
 * @param {Mount} m
 * @param {string} text
 */
function notice(m, text) {
  const div = document.createElement('div');
  div.className = 'tool-ui-notice';
  div.textContent = text;
  m.el.replaceChildren(div);
}

/** @param {Mount} m */
function run(m) {
  teardown(m);
  const render = renderers.get(m.kind);
  if (!render) {
    notice(m, host.label('missing', m.kind));
    return;
  }
  const gen = m.gen;
  const el = m.el;
  const ctx = {
    ...host.context(),
    kind: m.kind,
    callId: m.callId,
    // Only a live, on-screen component may speak for the user.
    send: (/** @type {string} */ text) => el.isConnected && host.send(text),
  };
  const fail = (/** @type {unknown} */ err) => {
    console.error(`lovia component "${m.kind}" failed to render:`, err);
    if (m.gen === gen && mounts.has(m)) notice(m, host.label('failed', m.kind));
  };
  let out;
  try {
    out = render(el, m.props, ctx);
  } catch (err) {
    fail(err);
    return;
  }
  Promise.resolve(out).then((cleanup) => {
    if (typeof cleanup !== 'function') return;
    // Superseded (re-render, unmount) while an async render was in flight.
    if (m.gen !== gen || !mounts.has(m)) safeCleanup(cleanup);
    else m.cleanup = cleanup;
  }, fail);
}

/**
 * Render `comp` into `el` — replacing whatever component `el` held.
 * @param {HTMLElement} el
 * @param {UiComponent} comp
 * @param {{ callId?: string | null }} [opts]
 */
export function mountComponent(el, comp, { callId = null } = {}) {
  unmountComponent(el);
  /** @type {Mount} */
  const m = { el, kind: comp.kind, props: comp.props, callId, cleanup: null, gen: 0 };
  mounts.add(m);
  run(m);
}

/**
 * Tear down the component in `el`, if any.
 * @param {HTMLElement} el
 */
export function unmountComponent(el) {
  for (const m of mounts) {
    if (m.el === el) {
      mounts.delete(m);
      teardown(m);
    }
  }
}

/** Re-render every component (e.g. after a theme switch). */
export function rerenderComponents() {
  for (const m of mounts) run(m);
}

/** Tear down components whose element has left the document. */
export function sweepComponents() {
  for (const m of mounts) {
    if (!m.el.isConnected) {
      mounts.delete(m);
      teardown(m);
    }
  }
}

/**
 * Sweep components as `root`'s subtree loses nodes — one hook for every path
 * that clears the transcript (session switch, history re-window, reset).
 * @param {HTMLElement | null} root
 */
export function watchComponents(root) {
  if (!root || typeof MutationObserver === 'undefined') return;
  new MutationObserver((records) => {
    if (!mounts.size) return;
    if (records.some((r) => r.removedNodes.length)) sweepComponents();
  }).observe(root, { childList: true, subtree: true });
}
