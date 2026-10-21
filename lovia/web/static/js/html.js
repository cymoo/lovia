// The built-in `html` UI component: {html, title?}, a self-contained page run
// in a sandboxed iframe. The `show_html` tool (lovia/web/html.py) produces it;
// any tool may, via `ui_result("html", props)`. Design and the browser facts
// it rests on: docs/design/web-html-component.md.
//
// The page is untrusted. It runs in sandbox.js's frame: an opaque origin (no
// cookies, storage, or parent DOM) inside a proxy that stops it navigating
// itself away; the CSP that heads its srcdoc blocks subresources and fetch.
//
// No DOM access at import time (the pure helpers are unit-tested under node).

import { registerRenderer } from './components.js';
import { figure, save, staticFigure } from './figure.js';
import { sandboxFrame } from './sandbox.js';
import { escapeHtml } from './util.js';

// MCP Apps' default policy, less 'self' (an opaque origin has none), plus
// form-action, which default-src doesn't cover — the sandbox stops forms in
// the frame, the policy in a downloaded copy.
export const FRAME_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
  "img-src data: blob:; font-src data:; media-src data: blob:; form-action 'none'";
// Longest follow-up a page may post; a longer one is dropped, not cut.
export const MAX_SEND = 4000;

// The chat's design tokens, handed to the page as CSS variables.
const TOKENS = [
  '--bg', '--surface', '--surface-2', '--border', '--ink', '--ink-2', '--muted',
  '--accent', '--accent-soft', '--accent-text', '--sans', '--mono', '--radius',
  '--chart-1', '--chart-2', '--chart-3', '--chart-4', '--chart-5', '--chart-6',
  '--chart-7', '--chart-8', '--chart-text', '--chart-title', '--chart-axis', '--chart-grid',
];

// Runs in the frame before the page's own code: reports the content height
// and offers `lovia.send(text)`. The parent trusts none of it — the page can
// post anything itself — so every message is checked there.
const BOOTSTRAP = `(() => {
  const post = (method, params) => parent.postMessage({ jsonrpc: '2.0', method, params }, '*');
  let last = -1;
  const report = () => {
    const height = Math.ceil(document.documentElement.getBoundingClientRect().height);
    if (height !== last) post('ui/notifications/size-changed', { height: (last = height) });
  };
  new ResizeObserver(report).observe(document.documentElement);
  addEventListener('load', report);
  // The proxy around the frame forbids navigation, so following a link would
  // only swap the page for an error page.
  addEventListener('click', (e) => {
    const a = e.target instanceof Element ? e.target.closest('a[href]') : null;
    if (a && !a.getAttribute('href').startsWith('#')) e.preventDefault();
  });
  window.lovia = {
    send: (text) => post('ui/message', { role: 'user', content: { type: 'text', text: String(text) } }),
  };
})();`;

/**
 * @typedef {{ html: string, title: string }} Page
 * @param {unknown} props
 * @returns {Page}
 */
export function normalizePage(props) {
  const p = /** @type {any} */ (props);
  if (!p || typeof p !== 'object' || typeof p.html !== 'string' || !p.html.trim()) {
    throw new Error('the page has no html');
  }
  return { html: p.html, title: typeof p.title === 'string' ? p.title : '' };
}

/**
 * The frame's document: our CSP, tokens, and bootstrap first, then the page.
 * A page that is a whole document still works — the parser folds its
 * html/head/body tags into ours.
 * @param {string} html
 * @param {{ theme: string, tokens: Record<string, string> }} opts
 * @returns {string}
 */
export function buildSrcdoc(html, { theme, tokens }) {
  // Token values come from our own stylesheet (or a deployer's); still, none
  // may close the <style> they sit in.
  const vars = Object.entries(tokens)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k}:${v.replace(/[<>]/g, '')}`)
    .join(';');
  return (
    `<!doctype html><html data-theme="${theme === 'dark' ? 'dark' : 'light'}"><head>` +
    '<meta charset="utf-8">' +
    `<meta http-equiv="Content-Security-Policy" content="${FRAME_CSP}">` +
    `<style>:root{${vars};color-scheme:${theme === 'dark' ? 'dark' : 'light'};` +
    'color:var(--ink);font-family:var(--sans)}</style>' +
    `<script>${BOOTSTRAP}</script></head><body>${html}`
  );
}

/**
 * Act on one message from the frame (already checked to come from it). The
 * names follow MCP Apps; anything else is ignored.
 * @param {unknown} data
 * @param {{ resize: (height: number) => void, send: (text: string) => unknown }} on
 */
export function handleMessage(data, on) {
  const d = /** @type {any} */ (data);
  if (!d || typeof d !== 'object' || d.jsonrpc !== '2.0') return;
  if (d.method === 'ui/notifications/size-changed') {
    const height = Number(d.params?.height);
    if (Number.isFinite(height) && height >= 0) on.resize(height);
  } else if (d.method === 'ui/message') {
    const c = d.params?.content;
    if (d.params?.role !== 'user' || c?.type !== 'text' || typeof c.text !== 'string') return;
    const text = c.text.trim();
    if (text && text.length <= MAX_SEND) on.send(text);
  }
}

/**
 * Route messages from `frame`, and only from it, to `on`. The origin of an
 * opaque frame is "null" and proves nothing; its window does.
 * @param {{ contentWindow: unknown }} frame
 * @param {Parameters<typeof handleMessage>[1]} on
 * @returns {() => void} Stop listening.
 */
export function listenToFrame(frame, on) {
  const onMessage = (/** @type {MessageEvent} */ e) => {
    if (e.source === frame.contentWindow) handleMessage(e.data, on);
  };
  window.addEventListener('message', onMessage);
  return () => window.removeEventListener('message', onMessage);
}

const LABELS = {
  en: {
    page: 'Page',
    download: 'Download HTML',
    run: 'Run',
    paused: 'This page runs scripts, so it waits for you when a chat is reopened.',
    exported: 'Interactive page — open the chat to use it.',
    source: 'Source',
  },
  zh: {
    page: '页面',
    download: '下载 HTML',
    run: '运行',
    paused: '该页面包含脚本，重新打开聊天时需要手动运行。',
    exported: '交互式页面——在聊天中打开才能使用。',
    source: '源码',
  },
};

/** @returns {Record<string, string>} */
function readTokens() {
  const cs = getComputedStyle(document.documentElement);
  return Object.fromEntries(TOKENS.map((t) => [t, cs.getPropertyValue(t).trim()]));
}

// Pages the user started from a paused card: a theme switch re-renders them,
// and they should not pause again.
const started = new WeakSet();

/** @type {import('./components.js').Render} */
function renderHtml(el, props, ctx) {
  const { html, title } = normalizePage(props); // throws → the failure notice
  const L = LABELS[ctx.lang] || LABELS.en;
  const { body, tool, filename } = figure(el, {
    kind: 'html',
    heading: title ? { text: title } : null,
    lang: ctx.lang,
  });
  const doc = () => buildSrcdoc(html, { theme: ctx.theme, tokens: readTokens() });
  // The document the frame runs, policy included: opened from disk, the
  // page still can't fetch or load anything remote.
  tool(L.download, 'HTML', () => save(new Blob([doc()], { type: 'text/html' }), filename('html')));

  /** @type {(() => void) | null} */
  let stop = null;
  const run = () => {
    started.add(el);
    const frame = sandboxFrame(doc(), {
      title: title || L.page,
      className: 'html-frame',
      colorScheme: ctx.theme === 'dark' ? 'dark' : 'light',
    });
    stop = listenToFrame(frame, {
      resize: (h) => frame.style.setProperty('--page-height', `${h}px`),
      // ctx.send wants a live user activation, but that is page-wide: a
      // keypress in the composer would let a page's timer post. Focus
      // inside the frame shows the gesture was the user's, in this page.
      send: (text) => document.activeElement === frame && ctx.send(text),
    });
    body.replaceChildren(frame);
  };

  // A replayed page waits for a click: one that loops forever would
  // otherwise freeze the chat each time it is opened.
  if (ctx.live || started.has(el)) {
    run();
  } else {
    const paused = document.createElement('div');
    paused.className = 'html-paused';
    const note = document.createElement('span');
    note.textContent = L.paused;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-primary btn-sm';
    btn.textContent = L.run;
    btn.addEventListener('click', run, { once: true });
    paused.append(note, btn);
    body.append(paused);
  }
  return () => stop?.();
}

/**
 * The export carries no scripts, so the page exports as a note and its
 * source.
 * @type {import('./components.js').Snapshot}
 */
export function htmlSnapshot(props, ctx) {
  const { html, title } = normalizePage(props);
  const L = LABELS[ctx.lang] || LABELS.en;
  return staticFigure(
    'html',
    title ? { text: title } : null,
    `<p>${escapeHtml(L.exported)}</p><details><summary>${escapeHtml(L.source)}</summary>` +
      `<pre><code class="language-html">${escapeHtml(html)}</code></pre></details>`,
  );
}

registerRenderer('html', { render: renderHtml, snapshot: htmlSnapshot });
