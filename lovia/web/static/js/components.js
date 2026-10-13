// UI components: a tool result rendered as UI instead of text.
//
// A tool opts in by returning a UI envelope (lovia/web/components.py
// `ui_result`) — {"lovia_ui": {"kind", "props" | "from_args", "data"?}} — and
// the renderer registered for `kind` draws it into the tool card, and
// optionally as static HTML into the chat's HTML export. With `data` (a ref
// from `put_ui_data`), the renderer gets the full props fetched from the chat
// store, and `props` — the preview — only when they are gone. Renderers come from the page
// itself or from `ChatUI(scripts=...)` modules calling
// `window.lovia.registerRenderer(kind, { render, snapshot })`. Design notes:
// docs/design/web-ui-components.md, docs/design/web-component-data.md.
//
// No DOM or app-module access at import time: the host (chat.js) injects
// labels and the render context through `configureComponents`, which keeps
// this module unit-testable under node.

/**
 * @typedef {object} UiComponent
 * @property {string} kind
 * @property {unknown} props The preview, when `data` is set.
 * @property {string} [data] Ref to the full props, stored with the chat.
 */
/** @typedef {() => void} Cleanup */
/**
 * @typedef {object} RenderContext
 * @property {string} kind
 * @property {'light' | 'dark'} theme
 * @property {'en' | 'zh'} lang
 * @property {string} basePath The mount prefix, for the deployer's own endpoints.
 * @property {string | null} sessionId
 * @property {string | null} callId
 * @property {boolean} live True when the result just arrived on the live
 *   stream, false when a reopened chat replays it.
 * @property {(text: string) => boolean} send Post a user message; needs a user gesture.
 */
/**
 * @typedef {(el: HTMLElement, props: unknown, ctx: RenderContext) =>
 *   void | Cleanup | Promise<void | Cleanup>} Render
 */
/** @typedef {Omit<RenderContext, 'send' | 'live'>} SnapshotContext */
/**
 * @typedef {(props: unknown, ctx: SnapshotContext) => string | Promise<string>} Snapshot
 *   Static HTML for the chat's HTML export — a standalone file, so no script;
 *   the export sanitizes it.
 */
/**
 * @typedef {object} Renderer
 * @property {Render} render Draw the component into the chat.
 * @property {Snapshot} [snapshot] Without one, the export keeps the raw output.
 */
/**
 * @typedef {object} Mount
 * @property {HTMLElement} el
 * @property {string} kind
 * @property {unknown} props
 * @property {string | null} callId
 * @property {boolean} live
 * @property {string | undefined} data
 * @property {Promise<unknown> | null} full The fetch of `data`, kept across re-renders.
 * @property {Cleanup | null} cleanup
 * @property {number} gen Bumped per render/teardown, so a late async cleanup
 *   knows it was superseded.
 */

// Mirror _KIND_RE / _REF_RE in lovia/web/components.py.
const KIND_RE = /^[a-z][a-z0-9_.-]{0,63}$/;
const REF_RE = /^[A-Za-z0-9_-]{16,64}$/;
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
  if ('props' in env) {
    const data = typeof env.data === 'string' && REF_RE.test(env.data) ? env.data : undefined;
    return data ? { kind: env.kind, props: env.props, data } : { kind: env.kind, props: env.props };
  }
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
  /** @type {() => Omit<RenderContext, 'kind' | 'callId' | 'live' | 'send'>} */
  context: () => ({ theme: 'light', lang: 'en', basePath: '', sessionId: null }),
  /** @type {(text: string) => boolean} */
  send: () => false,
  /** @type {(key: 'missing' | 'failed' | 'partial', kind: string) => string} */
  label: (key, kind) =>
    key === 'missing'
      ? `No renderer for “${kind}”`
      : key === 'partial'
        ? 'The full data is no longer available; showing a preview.'
        : 'Render failed',
};

/**
 * The full props stored under `ref` with the chat, or undefined when they're
 * gone (deleted with the chat, or unreachable).
 * @param {string} basePath
 * @param {string | null} sessionId
 * @param {string} ref
 * @returns {Promise<unknown>}
 */
async function loadData(basePath, sessionId, ref) {
  if (!sessionId) return undefined;
  const url = `${basePath}/api/sessions/${encodeURIComponent(sessionId)}/ui-data/${ref}`;
  try {
    const res = await fetch(url, { credentials: 'same-origin' });
    return res.ok ? await res.json() : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Wire the registry to the page: render context, message sending, notice
 * labels. Also publishes `window.lovia.registerRenderer` / `getRenderer`.
 * @param {Partial<typeof host>} opts
 */
export function configureComponents(opts) {
  Object.assign(host, opts);
  if (typeof window !== 'undefined') {
    window.lovia = Object.assign(window.lovia || {}, { registerRenderer, getRenderer });
  }
}

/**
 * The renderer registered for `kind` right now, so another can wrap it — e.g.
 * transform the props, then draw them with the built-in `chart`. Deliberately
 * the current object, not a lookup at call time: a wrapper re-registered under
 * the same kind must call the renderer it replaced, not itself.
 * @param {string} kind
 * @returns {Renderer | undefined}
 */
export function getRenderer(kind) {
  return renderers.get(kind);
}

/**
 * Register (or replace) the renderer for `kind`. Components of that kind
 * already on screen — waiting for it, or drawn by the one it replaces —
 * render again, so script load order never matters.
 * @param {string} kind
 * @param {Renderer} renderer
 */
export function registerRenderer(kind, renderer) {
  if (typeof kind !== 'string' || !KIND_RE.test(kind)) {
    throw new TypeError(`lovia.registerRenderer: invalid kind ${JSON.stringify(kind)}`);
  }
  if (typeof renderer?.render !== 'function') {
    throw new TypeError('lovia.registerRenderer: expected { render: function, snapshot?: function }');
  }
  if (renderer.snapshot != null && typeof renderer.snapshot !== 'function') {
    throw new TypeError('lovia.registerRenderer: snapshot must be a function');
  }
  renderers.set(kind, renderer);
  for (const m of mounts) if (m.kind === kind) run(m);
}

/**
 * `comp` as static HTML for the HTML export — from its full data when it has
 * some, else its preview — or null when its kind has no snapshot, there is
 * nothing to draw, or the snapshot fails: the export then keeps the raw output.
 * @param {UiComponent} comp
 * @param {{ callId?: string | null, sessionId?: string | null }} [opts]
 *   `sessionId`: the exported chat, which need not be the one on screen.
 * @returns {Promise<string | null>}
 */
export async function snapshotComponent(comp, { callId = null, sessionId = null } = {}) {
  const renderer = renderers.get(comp.kind);
  if (!renderer?.snapshot) return null;
  try {
    const ctx = { ...host.context(), kind: comp.kind, callId, sessionId };
    let props = comp.props;
    if (comp.data) {
      const full = await loadData(ctx.basePath, sessionId, comp.data);
      if (full !== undefined) props = full;
      else if (props === null) return null;
    }
    return String(await renderer.snapshot(props, ctx));
  } catch (err) {
    console.error(`lovia component "${comp.kind}" failed to snapshot:`, err);
    return null;
  }
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
  const renderer = renderers.get(m.kind);
  if (!renderer) {
    notice(m, host.label('missing', m.kind));
    return;
  }
  const gen = m.gen;
  const el = m.el;
  const ctx = {
    ...host.context(),
    kind: m.kind,
    callId: m.callId,
    live: m.live,
    // Only a live, on-screen component may speak for the user.
    send: (/** @type {string} */ text) => el.isConnected && host.send(text),
  };
  const current = () => m.gen === gen && mounts.has(m);
  const fail = (/** @type {unknown} */ err) => {
    console.error(`lovia component "${m.kind}" failed to render:`, err);
    if (current()) notice(m, host.label('failed', m.kind));
  };
  const draw = (/** @type {unknown} */ props, target = el) => {
    let out;
    try {
      out = renderer.render(target, props, ctx);
    } catch (err) {
      fail(err);
      return;
    }
    Promise.resolve(out).then((cleanup) => {
      if (typeof cleanup !== 'function') return;
      // Superseded (re-render, unmount) while an async render was in flight.
      if (current()) m.cleanup = cleanup;
      else safeCleanup(cleanup);
    }, fail);
  };
  if (!m.data) {
    draw(m.props);
    return;
  }
  const full = (m.full ??= loadData(ctx.basePath, ctx.sessionId, m.data));
  full.then((props) => {
    if (!current()) return;
    if (props !== undefined) {
      draw(props);
      return;
    }
    m.full = null; // a later render (a theme switch) may find it reachable again
    notice(m, host.label('partial', m.kind));
    if (m.props === null) return;
    // A box of its own: a renderer owns its element's children and may
    // replace them, notice included.
    const box = document.createElement('div');
    el.append(box);
    draw(m.props, box);
  });
}

/**
 * Render `comp` into `el` — replacing whatever component `el` held.
 * @param {HTMLElement} el
 * @param {UiComponent} comp
 * @param {{ callId?: string | null, live?: boolean }} [opts] `live`: the result
 *   just arrived on the stream, rather than a reopened chat replaying it.
 */
export function mountComponent(el, comp, { callId = null, live = false } = {}) {
  unmountComponent(el);
  /** @type {Mount} */
  const m = {
    el,
    kind: comp.kind,
    props: comp.props,
    callId,
    live,
    data: comp.data,
    full: null,
    cleanup: null,
    gen: 0,
  };
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
