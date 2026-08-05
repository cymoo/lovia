// util.js — tiny helpers shared across modules.
// (marked / DOMPurify / hljs are optional CDN globals — everything degrades.)
import { api } from './api.js';

/**
 * Escape HTML metacharacters so `s` renders as literal text, never markup.
 * @param {string} s
 * @returns {string}
 */
export function escapeHtml(s) {
  return String(s).replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
}

// ---- Markdown ------------------------------------------------------------
/**
 * Render markdown to sanitized HTML. Degrades to escaped plain text when
 * marked/DOMPurify aren't loaded (offline, blocked CDN, SRI failure).
 * Callers go through `renderMarkdownInto`, which also fixes up workspace refs.
 * @param {string} text
 * @returns {string} Sanitized HTML.
 */
function renderMarkdown(text) {
  if (!text.trim()) return '';
  // Never emit unsanitized HTML: without either library, escaped plain text
  // beats both a dead UI and an XSS hole.
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
    return `<p>${escapeHtml(text).replace(/\n/g, '<br>')}</p>`;
  }
  return DOMPurify.sanitize(marked.parse(text));
}

// ---- Workspace references inside markdown ----------------------------------
// Markdown written by the agent points at files the way the agent sees them —
// `![](uploads/0021.jpg)`. The app serves no such route, so the browser's own
// relative resolution 404s: workspace files are reachable only through
// /api/workspace/raw. Rewriting happens after sanitizing, on an inert tree,
// and only ever produces our own endpoint's URL.

// Left untouched: anything with a scheme (http:, data:, blob:), a
// protocol-relative URL, and the app's own paths (an already-rewritten src,
// or a bundled asset).
const EXTERNAL_SRC = /^(?:[a-z][a-z0-9+.-]*:|\/\/|\/(?:api|static)\/)/i;

/** Collapse `.` / `..` / empty segments into a plain relative path. */
function normalizeRel(path) {
  const parts = [];
  for (const part of path.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') parts.pop();
    else parts.push(part);
  }
  return parts.join('/');
}

/**
 * Workspace paths to try for one markdown reference, best reading first.
 *
 * Mirrors how the Files panel follows a markdown *link*: relative to the
 * document's own directory, then relative to the workspace root (authors —
 * and agents — mean either). An absolute path is passed through as-is first,
 * since the server resolves it and serves whatever lands inside the root;
 * stripping the leading slash covers the site-root convention.
 * @param {string} path Decoded, query/fragment-free reference.
 * @param {string} base Directory the markdown lives in (workspace-relative).
 * @returns {string[]}
 */
function workspaceCandidates(path, base) {
  const relToRoot = normalizeRel(path);
  if (path.startsWith('/')) return [path, relToRoot].filter(Boolean);
  const relToDoc = base ? normalizeRel(`${base}/${path}`) : relToRoot;
  return [...new Set([relToDoc, relToRoot])].filter(Boolean);
}

/**
 * Point workspace-relative `<img>` sources at the raw-bytes endpoint.
 * @param {ParentNode} root Container of freshly rendered markdown.
 * @param {{ agent?: string, base?: string }} [opts]
 */
function resolveWorkspaceImages(root, { agent, base = '' } = {}) {
  root.querySelectorAll('img[src]').forEach((/** @type {HTMLImageElement} */ img) => {
    const src = img.getAttribute('src') || '';
    if (!src || EXTERNAL_SRC.test(src)) return;
    let path = src.split('#')[0].split('?')[0];
    // marked runs encodeURI over hrefs, so a CJK or spaced filename arrives
    // percent-encoded — and the query builder encodes again. Decode first or
    // the server looks up a file literally named "%E5%9B%BE...".
    try {
      path = decodeURIComponent(path);
    } catch {
      /* malformed escape — use it verbatim */
    }
    const candidates = workspaceCandidates(path, base);
    if (!candidates.length) return;
    let i = 0;
    const show = () => {
      img.src = api.workspaceRawUrl({ agent, path: candidates[i] });
    };
    // 404 (wrong base) / 403 (outside the root) → try the next reading before
    // giving up and showing the browser's broken-image glyph.
    if (candidates.length > 1) {
      img.addEventListener('error', () => {
        if (++i < candidates.length) show();
      });
    }
    show();
  });
}

/**
 * Render markdown into `el`, resolving workspace-relative image sources.
 *
 * Parses into an inert `<template>` first: images there don't load, so the
 * un-rewritten (404-bound) src is never requested — the corrected one is the
 * only fetch the browser makes.
 * @param {Element} el Target; its children are replaced.
 * @param {string} text Markdown source.
 * @param {{ agent?: string, base?: string }} [opts] `base` is the directory
 *   the markdown lives in — '' (the workspace root) for chat replies.
 */
export function renderMarkdownInto(el, text, opts = {}) {
  const tmpl = document.createElement('template');
  tmpl.innerHTML = renderMarkdown(text);
  resolveWorkspaceImages(tmpl.content, opts);
  el.replaceChildren(tmpl.content);
}

// ---- Syntax highlighting ---------------------------------------------------
// Highlighted-HTML cache. Streaming re-renders replace whole DOM subtrees,
// which would re-run hljs over every block each flush — O(blocks) of real
// parsing work per tick. Keyed by class+source, each unique block is parsed
// once; repeat renders are an innerHTML assignment.
const _hljsCache = new Map();

/**
 * Cached syntax-highlight pass over every `<pre><code>` in `container` (no
 * chrome — callers add their own copy buttons etc.; mermaid blocks are skipped).
 * @param {Element} container
 */
export function highlightIn(container) {
  if (typeof hljs === 'undefined') return;
  container.querySelectorAll('pre code').forEach((/** @type {HTMLElement} */ el) => {
    if (el.classList.contains('language-mermaid')) return; // rendered as a diagram instead
    if (el.dataset.highlighted) return;
    const key = `${el.className}\u0000${el.textContent}`;
    const hit = _hljsCache.get(key);
    if (hit) {
      el.innerHTML = hit.html;
      el.className = hit.className; // hljs adds its own classes; restore them too
    } else {
      hljs.highlightElement(el);
      if (_hljsCache.size > 500) _hljsCache.clear(); // cheap bound, rarely hit
      _hljsCache.set(key, { html: el.innerHTML, className: el.className });
    }
    el.dataset.highlighted = '1';
  });
}

// ---- Sizes -----------------------------------------------------------------
/**
 * Human-readable byte size, e.g. 2048 → "2.0 KB".
 * @param {number | null | undefined} n
 * @returns {string} Formatted size, or "" if not a finite number.
 */
export function formatBytes(n) {
  if (n == null || !Number.isFinite(n)) return '';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB'];
  let v = n;
  for (const u of units) {
    v /= 1024;
    if (v < 1024 || u === 'GB') return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${u}`;
  }
  return '';
}

/**
 * Coerce a backend timestamp to a Date. Accepts epoch seconds (floats from the
 * backend) or milliseconds.
 * @param {number} ts
 * @returns {Date}
 */
export function toDate(ts) {
  return new Date(ts > 1e12 ? ts : ts * 1000);
}

const pad = (n) => String(n).padStart(2, '0');

/**
 * Full form: "2026-07-05 14:32" (+":07" with seconds). Tooltips, schedules.
 * @param {number | null} ts
 * @param {{ seconds?: boolean }} [opts]
 * @returns {string}
 */
export function formatDateTime(ts, { seconds = false } = {}) {
  if (ts == null) return '';
  const d = toDate(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  const base =
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return seconds ? `${base}:${pad(d.getSeconds())}` : base;
}

/**
 * Compact timeline stamp: "14:32" today, "07-01 09:15" this year,
 * "2025-12-31 23:59" otherwise. Pair with formatDateTime in a tooltip.
 * @param {number | null} ts
 * @returns {string}
 */
export function formatTimeSmart(ts) {
  if (ts == null) return '';
  const d = toDate(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  const now = new Date();
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return hm;
  const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  if (d.getFullYear() === now.getFullYear()) return `${md} ${hm}`;
  return `${d.getFullYear()}-${md} ${hm}`;
}

// ---- Attachments -----------------------------------------------------------
// Browser-renderable image extensions. Mirrors the server's PREVIEW_IMAGE_EXT
// (lovia/web/media.py) EXACTLY — keep the two in sync. SVG is excluded: it can
// carry scripts and is never served inline, so it's treated as a file here.
export const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'bmp', 'ico']);

/**
 * True when `path`'s extension is a browser-renderable image (see IMAGE_EXT).
 * @param {string} path
 * @returns {boolean}
 */
export function isImagePath(path) {
  return IMAGE_EXT.has((String(path).split('.').pop() || '').toLowerCase());
}
