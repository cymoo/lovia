// util.js — tiny dependency-free helpers shared across modules.
// (marked / DOMPurify / hljs are optional CDN globals — everything degrades.)

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
 * @param {string} text
 * @returns {string} Sanitized HTML.
 */
export function renderMarkdown(text) {
  if (!text.trim()) return '';
  // Never emit unsanitized HTML: without either library, escaped plain text
  // beats both a dead UI and an XSS hole.
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
    return `<p>${escapeHtml(text).replace(/\n/g, '<br>')}</p>`;
  }
  return DOMPurify.sanitize(marked.parse(text));
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
