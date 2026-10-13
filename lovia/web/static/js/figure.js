// The card the built-in UI components (charts.js, tables.js) draw in: an HTML
// heading, a toolbar revealed on hover, the component's body, and full screen.
// Plus the download helpers they share, and the static card of their export
// snapshots.
//
// No DOM access at import time (the pure helpers are unit-tested under node).

import { exportFilename } from './export.js';
import { icon } from './icons.js';
import { toast } from './toast.js';
import { escapeHtml } from './util.js';

const LABELS = {
  en: { full: 'Full screen', exitFull: 'Exit full screen', failed: 'That didn’t work' },
  zh: { full: '全屏查看', exitFull: '退出全屏', failed: '操作失败' },
};

/**
 * @typedef {object} Figure
 * @property {HTMLElement} fig The card — what goes full screen.
 * @property {HTMLElement} body Where the component draws.
 * @property {(label: string, face: string, action: () => unknown) => HTMLButtonElement} tool
 *   Add a toolbar button (before full screen). A failing action is logged and toasted.
 * @property {(ext: string) => string} filename A download name from the heading.
 */

/**
 * Build a card into `el`. `kind` is added as a class, for the component's own
 * styles (`.chart .figure-body`, …).
 * @param {HTMLElement} el
 * @param {{ kind: string, heading: { text: string, subtext?: string } | null, lang: string }} opts
 * @returns {Figure}
 */
export function figure(el, { kind, heading, lang }) {
  const L = LABELS[/** @type {'en' | 'zh'} */ (lang)] || LABELS.en;
  const fig = document.createElement('div');
  fig.className = `figure ${kind}`;
  const head = document.createElement('div');
  head.className = 'figure-head';
  if (heading) {
    const titles = document.createElement('div');
    titles.className = 'figure-heading';
    for (const [cls, text] of [['figure-title', heading.text], ['figure-subtitle', heading.subtext]]) {
      if (!text) continue;
      const div = document.createElement('div');
      div.className = cls;
      div.textContent = text;
      titles.append(div);
    }
    head.append(titles);
    fig.setAttribute('role', 'figure');
    fig.setAttribute('aria-label', heading.text);
  }
  const tools = document.createElement('div');
  tools.className = 'figure-tools';
  head.append(tools);
  const body = document.createElement('div');
  body.className = 'figure-body';
  fig.append(head, body);
  el.append(fig);

  /** @type {HTMLButtonElement | null} */
  let full = null;
  /** @type {Figure['tool']} */
  const tool = (label, face, action) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'tool-action';
    b.title = label;
    b.setAttribute('aria-label', label);
    b.textContent = face;
    b.addEventListener('click', () => {
      Promise.resolve()
        .then(action)
        .catch((err) => {
          console.error(`lovia ${kind} action failed:`, err);
          toast(L.failed, { type: 'error' });
        });
    });
    tools.insertBefore(b, full);
    return b;
  };
  if (document.fullscreenEnabled) {
    full = tool(L.full, '', () =>
      document.fullscreenElement === fig ? document.exitFullscreen() : fig.requestFullscreen(),
    );
    const btn = full;
    const sync = () => {
      const on = document.fullscreenElement === fig;
      btn.title = on ? L.exitFull : L.full;
      btn.setAttribute('aria-label', btn.title);
      btn.innerHTML = icon(on ? 'minimize-2' : 'maximize-2', { size: 12 });
    };
    fig.addEventListener('fullscreenchange', sync);
    sync();
  }
  const filename = (/** @type {string} */ ext) => exportFilename(heading?.text || kind, ext);
  return { fig, body, tool, filename };
}

/**
 * A snapshot's card for the HTML export: the heading as a caption over `inner`
 * (already-escaped HTML).
 * @param {string} kind
 * @param {{ text: string, subtext?: string } | null} heading
 * @param {string} inner
 * @returns {string}
 */
export function staticFigure(kind, heading, inner) {
  const caption = heading
    ? `<figcaption>${escapeHtml(heading.text)}` +
      (heading.subtext ? `<span>${escapeHtml(heading.subtext)}</span>` : '') +
      '</figcaption>'
    : '';
  return `<figure class="${kind}">${caption}${inner}</figure>`;
}

/**
 * RFC 4180 CSV. Strings that a spreadsheet would run as a formula are
 * prefixed with `'` — the data is untrusted.
 * @param {unknown[][]} rows
 * @returns {string}
 */
export function toCsv(rows) {
  const cell = (/** @type {unknown} */ v) => {
    let s = v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);
    if (typeof v === 'string' && /^[=+\-@\t\r]/.test(s)) s = `'${s}`;
    return /[",\r\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
  };
  return rows.map((r) => r.map(cell).join(',')).join('\r\n');
}

/**
 * `rows` as a CSV file. The BOM makes spreadsheet apps read it as UTF-8 (CJK).
 * @param {unknown[][]} rows
 */
export function csvBlob(rows) {
  const bom = new Uint8Array([0xef, 0xbb, 0xbf]);
  return new Blob([bom, toCsv(rows)], { type: 'text/csv;charset=utf-8' });
}

/**
 * @param {Blob} blob
 * @param {string} filename
 */
export function save(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url));
}
