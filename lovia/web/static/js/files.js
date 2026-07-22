// Files panel — a read-only window into the agent's workspace.
//
// lovia is a personal assistant, so the panel leads with "Recent": a flat,
// newest-first list across the whole workspace — the thing the assistant just
// wrote floats to the top. "Browse" (breadcrumb + one directory level) is the
// secondary view. The viewer renders documents as documents: markdown rich by
// default, images inline, CSV as a table; code gets highlighting.
//
// Wiring: chat.js emits `workspace-file-touched` for write_file/edit_file
// tool calls (live and replayed history) and `workspace-maybe-stale` after a
// shell run; this module owns everything else.
import { t } from './i18n.js';
import { store } from './store.js';
import { api } from './api.js';
import { copyToClipboard, setSidebarAutoCollapsed } from './ui.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { formatBytes, formatTimeSmart, highlightIn, renderMarkdown } from './util.js';

// Browser-renderable image previews. This set mirrors the server's
// PREVIEW_IMAGE_EXT (lovia/web/media.py) EXACTLY — keep the two in sync. SVG is
// excluded: it can carry scripts and is never served inline, so it renders as
// source text here instead.
const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'bmp', 'ico']);
const MD_EXT = new Set(['md', 'markdown']);
const CSV_EXT = new Set(['csv', 'tsv']);
const CSV_MAX_ROWS = 500;

const els = {};
const state = {
  available: false, // current agent has a workspace
  open: false,
  mode: 'recent', // 'recent' | 'browse'
  browsePath: '', // '' = workspace root
  entries: [],
  touched: new Set(), // paths this chat's write_file/edit_file produced
  // Per-path edit counter — busts the browser's in-page image cache only when
  // the agent actually touched the file. First opens use the bare URL, so the
  // HTTP cache (revalidated via the server's ETag/no-cache) does its job.
  revs: new Map(),
  stale: false, // a shell run may have changed files
  filter: '', // case-insensitive substring over listed paths
  wrap: localStorage.getItem('lovia-files-wrap') !== '0', // wrap long lines
  viewing: null, // { path, kind, raw, name, end, totalLines, truncated }
};

const ext = (path) => (path.split('.').pop() || '').toLowerCase();
const basename = (path) => path.split('/').pop() || path;
const dirname = (path) => (path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '');

// Tool args may carry the path relative to the root (like listings) or
// absolute — treat a listing entry as touched when either form matches.
function isTouched(entryPath) {
  for (const p of state.touched) {
    if (entryPath === p || p.endsWith(`/${entryPath}`)) return true;
  }
  return false;
}

// Sum of edit revisions for a path (same relative/absolute matching as
// isTouched); 0 = never touched this chat → cacheable bare URL.
function revOf(path) {
  let n = 0;
  for (const [p, r] of state.revs) {
    if (p === path || p.endsWith(`/${path}`)) n += r;
  }
  return n;
}

// ---- Panel sizing -----------------------------------------------------------
// The divider on the panel's left edge drags the width (arrow keys nudge it,
// double-click resets). The width lives in a `--files-w` CSS var and persists
// per browser; unset, the stylesheet default applies.
const WIDTH_KEY = 'lovia-files-w';
const MIN_W = 300;
const RESERVED_W = 520; // keep at least this much viewport for the chat column

const isPhone = () => window.matchMedia('(max-width: 720px)').matches;
const clampW = (w) =>
  Math.min(Math.max(Math.round(w), MIN_W), Math.max(MIN_W, window.innerWidth - RESERVED_W));
const panelWidth = () => els.panel.getBoundingClientRect().width;

function applyWidth(px) {
  if (px == null) document.documentElement.style.removeProperty('--files-w');
  else document.documentElement.style.setProperty('--files-w', `${clampW(px)}px`);
}

// Three columns need room. While the panel is open on a viewport too tight
// for sidebar + panel + a comfortable chat column, it claims the sidebar's
// space; the claim is released on close (and beaten by an explicit expand —
// the two layers live in ui.js).
function claimSpace() {
  if (isPhone()) return; // the phone drawer overlays, it doesn't push
  if (!state.open) {
    setSidebarAutoCollapsed(false);
    return;
  }
  const sidebarW =
    parseInt(
      getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w'),
      10,
    ) || 272;
  setSidebarAutoCollapsed(window.innerWidth < sidebarW + panelWidth() + 760);
}

function initResizer() {
  const saved = Number(localStorage.getItem(WIDTH_KEY));
  if (saved) applyWidth(saved);

  const rz = els.resizer;
  let startX = 0;
  let startW = 0;

  const persistWidth = () => {
    localStorage.setItem(WIDTH_KEY, String(Math.round(panelWidth())));
    claimSpace();
  };

  rz.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    startX = e.clientX;
    startW = panelWidth();
    rz.setPointerCapture(e.pointerId);
    document.body.classList.add('files-resizing');
  });
  rz.addEventListener('pointermove', (e) => {
    if (!rz.hasPointerCapture(e.pointerId)) return;
    applyWidth(startW + (startX - e.clientX)); // panel sits right: left = wider
  });
  const endDrag = (e) => {
    if (!rz.hasPointerCapture(e.pointerId)) return;
    rz.releasePointerCapture(e.pointerId);
    document.body.classList.remove('files-resizing');
    persistWidth();
  };
  rz.addEventListener('pointerup', endDrag);
  rz.addEventListener('pointercancel', endDrag);

  rz.addEventListener('dblclick', () => {
    applyWidth(null);
    localStorage.removeItem(WIDTH_KEY);
    claimSpace();
  });
  rz.addEventListener('keydown', (e) => {
    const step = e.key === 'ArrowLeft' ? 24 : e.key === 'ArrowRight' ? -24 : 0;
    if (!step) return;
    e.preventDefault();
    applyWidth(panelWidth() + step);
    persistWidth();
  });

  // A window resize can re-tighten (or free) the space the open panel needs.
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(claimSpace, 120);
  });
}

// ---- List / preview split ---------------------------------------------------
// The divider between the file list and the preview drags the split (arrow
// keys nudge it, double-click resets) — mirrors initResizer. The viewer height
// lives in a `--files-viewer-h` CSS var as a *percentage* of the panel (px
// would crush the list when the window shrinks) and persists per browser.
const SPLIT_KEY = 'lovia-files-split';
const MIN_VIEWER_H = 240; // matches .files-viewer's min-height
const MIN_LIST_H = 100; // keep some list visible above the preview

const viewerHeight = () => els.viewer.getBoundingClientRect().height;

function applySplit(pct) {
  if (pct == null) document.documentElement.style.removeProperty('--files-viewer-h');
  else document.documentElement.style.setProperty('--files-viewer-h', `${pct}%`);
}

// Clamp a proposed viewer height (px) so both halves stay usable, then apply
// it as a percentage of the panel.
function setViewerPx(px) {
  const panel = els.panel.getBoundingClientRect();
  if (!panel.height) return;
  const listTop = els.list.getBoundingClientRect().top - panel.top;
  const max = panel.height - listTop - MIN_LIST_H;
  const clamped = Math.min(Math.max(px, MIN_VIEWER_H), Math.max(MIN_VIEWER_H, max));
  applySplit((clamped / panel.height) * 100);
}

// Surface the split position to assistive tech (the separator is focusable
// and keyboard-resizable): value = the preview's share of the panel, 0–100.
function syncSplitAria() {
  const total = els.panel.getBoundingClientRect().height;
  if (!total) return;
  els.split.setAttribute(
    'aria-valuenow',
    String(Math.round((viewerHeight() / total) * 100)),
  );
}

// Re-clamp a restored split against the panel's current geometry: the saved
// percentage was applied before the preview was ever visible, so it may
// violate the px minimums at this panel size (a resize can do the same).
// The stylesheet default is always in bounds — leave the var unset then.
function clampSplit() {
  if (els.viewer.classList.contains('hidden')) return;
  if (document.documentElement.style.getPropertyValue('--files-viewer-h')) {
    setViewerPx(viewerHeight());
  }
  syncSplitAria();
}

function initSplit() {
  // Ignore garbage/extreme saved values — the stylesheet default is fine.
  const saved = Number(localStorage.getItem(SPLIT_KEY));
  if (saved >= 15 && saved <= 90) applySplit(saved);

  const sp = els.split;
  let startY = 0;
  let startH = 0;

  const persistSplit = () => {
    const total = els.panel.getBoundingClientRect().height;
    if (!total) return;
    // Re-read the rendered height: CSS min-height may have clamped harder.
    localStorage.setItem(SPLIT_KEY, ((viewerHeight() / total) * 100).toFixed(1));
    syncSplitAria();
  };

  sp.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    startY = e.clientY;
    startH = viewerHeight();
    sp.setPointerCapture(e.pointerId);
    document.body.classList.add('files-splitting');
  });
  sp.addEventListener('pointermove', (e) => {
    if (!sp.hasPointerCapture(e.pointerId)) return;
    setViewerPx(startH + (startY - e.clientY)); // viewer sits below: up = taller
  });
  const endDrag = (e) => {
    if (!sp.hasPointerCapture(e.pointerId)) return;
    sp.releasePointerCapture(e.pointerId);
    document.body.classList.remove('files-splitting');
    persistSplit();
  };
  sp.addEventListener('pointerup', endDrag);
  sp.addEventListener('pointercancel', endDrag);

  sp.addEventListener('dblclick', () => {
    applySplit(null);
    localStorage.removeItem(SPLIT_KEY);
    syncSplitAria();
  });
  sp.addEventListener('keydown', (e) => {
    const step = e.key === 'ArrowUp' ? 24 : e.key === 'ArrowDown' ? -24 : 0;
    if (!step) return;
    e.preventDefault();
    setViewerPx(viewerHeight() + step);
    persistSplit();
  });

  // A window resize can push the restored percentage past the px minimums.
  let splitTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(splitTimer);
    splitTimer = setTimeout(clampSplit, 120);
  });
}

// ---- Panel open/close -----------------------------------------------------
// `persist: false` is for forced closes (agent without a workspace) — they
// must not overwrite the user's remembered open/closed preference.
function setOpen(open, { persist = true } = {}) {
  state.open = open && state.available;
  els.panel.classList.toggle('open', state.open);
  els.btn?.setAttribute('aria-expanded', String(state.open));
  if (persist) localStorage.setItem('lovia-files-open', state.open ? '1' : '0');
  claimSpace();
  if (state.open) refresh();
}

function updateVisibility() {
  const agent = store.agents.find((a) => a.name === store.agent);
  state.available = !!agent?.workspace;
  els.btn?.classList.toggle('hidden', !state.available);
  const phone = window.matchMedia('(max-width: 720px)').matches;
  if (!state.available) {
    setOpen(false, { persist: false });
  } else if (
    !phone && // on phones the panel is a transient drawer — never auto-open
    localStorage.getItem('lovia-files-open') === '1' &&
    !state.open
  ) {
    setOpen(true);
  }
}

// ---- Lists ------------------------------------------------------------------
async function refresh() {
  if (!state.open) return;
  state.stale = false;
  els.refresh.classList.remove('stale');
  try {
    if (state.mode === 'recent') {
      state.entries = await api.workspaceRecent({ agent: store.agent });
    } else {
      state.entries = await api.workspaceFiles({
        agent: store.agent,
        path: state.browsePath || '.',
      });
    }
  } catch (err) {
    els.list.replaceChildren(emptyNode(err.message || t('files.loadFailed')));
    return;
  }
  renderCrumbs();
  renderList();
}

function emptyNode(text) {
  const div = document.createElement('div');
  div.className = 'files-empty';
  div.textContent = text;
  return div;
}

function setMode(mode) {
  state.mode = mode;
  els.tabRecent.classList.toggle('active', mode === 'recent');
  els.tabRecent.setAttribute('aria-selected', String(mode === 'recent'));
  els.tabBrowse.classList.toggle('active', mode === 'browse');
  els.tabBrowse.setAttribute('aria-selected', String(mode === 'browse'));
  els.crumbs.classList.toggle('hidden', mode !== 'browse');
  refresh();
}

function renderCrumbs() {
  if (state.mode !== 'browse') return;
  els.crumbs.replaceChildren();
  const parts = state.browsePath ? state.browsePath.split('/') : [];
  const crumb = (label, path, last) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'files-crumb';
    b.textContent = label;
    if (last) b.disabled = true;
    else b.addEventListener('click', () => { state.browsePath = path; refresh(); });
    return b;
  };
  els.crumbs.appendChild(crumb('~', '', parts.length === 0));
  parts.forEach((part, i) => {
    const sep = document.createElement('span');
    sep.className = 'files-crumb-sep';
    sep.textContent = '/';
    els.crumbs.appendChild(sep);
    els.crumbs.appendChild(
      crumb(part, parts.slice(0, i + 1).join('/'), i === parts.length - 1),
    );
  });
}

function rowIcon(entry) {
  if (entry.is_dir) return icon('folder', { size: 15 });
  const e = ext(entry.path);
  if (IMAGE_EXT.has(e)) return icon('image', { size: 15 });
  if (MD_EXT.has(e) || e === 'txt') return icon('file-text', { size: 15 });
  return icon('file', { size: 15 });
}

function renderList() {
  const frag = document.createDocumentFragment();
  const needle = state.filter.toLowerCase();
  const entries = needle
    ? state.entries.filter((e) => e.path.toLowerCase().includes(needle))
    : state.entries;
  if (!entries.length) {
    frag.appendChild(
      emptyNode(
        needle
          ? t('files.noMatch')
          : state.mode === 'recent'
            ? t('files.noFiles')
            : t('files.emptyDir'),
      ),
    );
  }
  for (const entry of entries) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'file-row';
    if (!entry.is_dir && isTouched(entry.path)) row.classList.add('touched');
    if (state.viewing?.path === entry.path) row.classList.add('active');

    const ic = document.createElement('span');
    ic.className = 'file-row-icon';
    ic.innerHTML = rowIcon(entry);

    const main = document.createElement('span');
    main.className = 'file-row-main';
    const name = document.createElement('span');
    name.className = 'file-row-name';
    name.textContent = basename(entry.path);
    const sub = document.createElement('span');
    sub.className = 'file-row-sub';
    const bits = [];
    if (state.mode === 'recent' && dirname(entry.path)) bits.push(dirname(entry.path));
    if (entry.is_dir) bits.push(t('files.folder'));
    else if (entry.size != null) bits.push(formatBytes(entry.size));
    if (entry.mtime) bits.push(formatTimeSmart(entry.mtime));
    if (entry.symlink_target) bits.push('→ ' + entry.symlink_target);
    sub.textContent = bits.join(' · ');
    main.append(name, sub);

    row.append(ic, main);
    row.title = entry.path;
    row.addEventListener('click', () => {
      if (entry.is_dir) {
        state.mode === 'browse' || setMode('browse');
        state.browsePath = entry.path;
        refresh();
      } else {
        openFile(entry.path);
      }
    });
    frag.appendChild(row);
  }
  els.list.replaceChildren(frag);
}

// ---- Viewer -------------------------------------------------------------------
function closeViewer() {
  state.viewing = null;
  els.viewer.classList.add('hidden');
  els.split?.classList.add('hidden');
  els.viewerBody.replaceChildren();
  renderList(); // drop the active highlight
}

function viewerNote(text, action) {
  const note = document.createElement('div');
  note.className = 'files-note';
  const span = document.createElement('span');
  span.textContent = text;
  note.appendChild(span);
  if (action) note.appendChild(action);
  return note;
}

// Markdown/CSV reuse the transcript's typography by rendering inside the same
// .turn > .body wrapper the chat uses (see styles.css).
function bodyWrapper() {
  const turn = document.createElement('div');
  turn.className = 'turn';
  const body = document.createElement('div');
  body.className = 'body';
  turn.appendChild(body);
  return { turn, body };
}

// Minimal quote-aware CSV/TSV parser (v1: good enough for assistant output).
function parseDelimited(text, delim) {
  const rows = [];
  let row = [];
  let field = '';
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else quoted = false;
      } else field += c;
    } else if (c === '"') {
      quoted = true;
    } else if (c === delim) {
      row.push(field); field = '';
    } else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(field); field = '';
      rows.push(row); row = [];
    } else {
      field += c;
    }
    if (rows.length > CSV_MAX_ROWS) break;
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function renderCsv(content, delim) {
  const { turn, body } = bodyWrapper();
  const rows = parseDelimited(content, delim);
  if (rows.length < 2) return null; // not table-shaped — fall back to text
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const headTr = document.createElement('tr');
  for (const cell of rows[0]) {
    const th = document.createElement('th');
    th.textContent = cell;
    headTr.appendChild(th);
  }
  thead.appendChild(headTr);
  const tbody = document.createElement('tbody');
  for (const r of rows.slice(1, CSV_MAX_ROWS + 1)) {
    const tr = document.createElement('tr');
    for (const cell of r) {
      const td = document.createElement('td');
      td.textContent = cell;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.append(thead, tbody);
  body.appendChild(table);
  return turn;
}

function renderText(content, path) {
  const pre = document.createElement('pre');
  const code = document.createElement('code');
  const e = ext(path);
  if (e && /^[a-z0-9]+$/.test(e)) code.className = `language-${e}`;
  code.textContent = content;
  pre.appendChild(code);
  // highlightIn queries `pre code` within a container — use a throwaway one.
  const holder = document.createElement('div');
  holder.appendChild(pre);
  highlightIn(holder);
  return pre;
}

// Returns false when the file couldn't be read (so link-following can retry
// with a different base); true otherwise.
async function openFile(path, { silent = false } = {}) {
  const name = basename(path);
  const viewerWasHidden = els.viewer.classList.contains('hidden');
  els.viewer.classList.remove('hidden');
  els.split?.classList.remove('hidden');
  // First show: the restored split was applied blind (panel geometry unknown
  // until now) — re-clamp it, and give the separator its initial aria value.
  if (viewerWasHidden && els.split) clampSplit();
  els.viewerName.textContent = path;
  els.viewerName.title = path;
  els.download.href = api.workspaceRawUrl({ agent: store.agent, path, download: true });
  els.mdToggle.classList.add('hidden');
  els.wrapToggle?.classList.add('hidden'); // renderViewerContent re-shows for text

  const kind = IMAGE_EXT.has(ext(path))
    ? 'image'
    : MD_EXT.has(ext(path))
      ? 'md'
      : CSV_EXT.has(ext(path))
        ? 'csv'
        : 'text';
  const keepRaw = silent && state.viewing?.path === path ? state.viewing.raw : false;
  state.viewing = { path, kind, name, raw: keepRaw };
  if (!silent) renderList();

  if (kind === 'image') {
    const img = document.createElement('img');
    img.className = 'files-img';
    img.alt = name;
    const rev = revOf(path);
    img.src =
      api.workspaceRawUrl({ agent: store.agent, path }) + (rev ? `&v=${rev}` : '');
    els.viewerBody.replaceChildren(img);
    return true;
  }

  let data;
  try {
    data = await api.workspaceFile({ agent: store.agent, path });
  } catch (err) {
    els.viewerBody.replaceChildren(viewerNote(err.message || t('files.readFailed')));
    return false;
  }
  if (state.viewing?.path !== path) return true; // user opened something else meanwhile

  if (data.binary) {
    const dl = document.createElement('a');
    dl.className = 'btn btn-ghost btn-sm';
    dl.textContent = t('files.download');
    dl.href = api.workspaceRawUrl({ agent: store.agent, path, download: true });
    dl.setAttribute('download', '');
    els.viewerBody.replaceChildren(
      viewerNote(t('files.binary'), dl),
    );
    return true;
  }

  state.viewing.end = data.end;
  state.viewing.totalLines = data.total_lines;
  state.viewing.truncated = data.truncated;
  state.viewing.content = data.content;
  renderViewerContent();
  return true;
}

// Links inside rendered markdown: keep the user in the app. Externals open a
// new tab; relative hrefs open in the viewer — resolved against the current
// file's directory first, then (authors often mean root-relative) the root.
async function followViewerLink(href) {
  const base = dirname(state.viewing?.path || '');
  const joined = href.startsWith('/')
    ? href.slice(1)
    : (base ? `${base}/` : '') + href;
  const parts = [];
  for (const part of joined.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') parts.pop();
    else parts.push(part);
  }
  const fileRelative = parts.join('/');
  const rootRelative = href.replace(/^\.?\//, '');
  if (await openFile(fileRelative)) return;
  if (rootRelative !== fileRelative) await openFile(rootRelative);
}

function syncWrapButton() {
  els.wrapToggle.innerHTML = icon('wrap-text', { size: 15 });
  els.wrapToggle.title = state.wrap ? t('files.nowrap') : t('files.wrap');
  els.wrapToggle.classList.toggle('active', state.wrap);
}

function renderViewerContent() {
  const v = state.viewing;
  if (!v) return;
  els.viewerBody.replaceChildren();
  els.viewerBody.classList.toggle('nowrap', !state.wrap);
  // Wrap toggling only means something for plain text / raw views.
  const textual = v.kind === 'text' || (v.kind === 'md' && v.raw) || v.kind === 'csv';
  els.wrapToggle.classList.toggle('hidden', !textual);

  if (v.truncated) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'btn btn-ghost btn-sm';
    more.textContent = t('files.loadMore');
    more.addEventListener('click', loadMore);
    els.viewerBody.appendChild(
      viewerNote(t('files.showingLines', { end: v.end, total: v.totalLines }), more),
    );
  }

  if (v.kind === 'md') {
    els.mdToggle.classList.remove('hidden');
    els.mdToggle.innerHTML = icon(v.raw ? 'file-text' : 'code', { size: 15 });
    els.mdToggle.title = v.raw ? t('files.rendered') : t('files.raw');
    if (!v.raw) {
      const { turn, body } = bodyWrapper();
      body.innerHTML = renderMarkdown(v.content);
      highlightIn(body);
      els.viewerBody.appendChild(turn);
      return;
    }
  }
  if (v.kind === 'csv' && !v.truncated) {
    const table = renderCsv(v.content, ext(v.path) === 'tsv' ? '\t' : ',');
    if (table) {
      els.viewerBody.appendChild(table);
      return;
    }
  }
  els.viewerBody.appendChild(renderText(v.content, v.path));
}

async function loadMore() {
  const v = state.viewing;
  if (!v || !v.truncated) return;
  try {
    const data = await api.workspaceFile({
      agent: store.agent,
      path: v.path,
      start: v.end + 1,
    });
    if (state.viewing !== v) return;
    v.content += (v.content.endsWith('\n') ? '' : '\n') + data.content;
    v.end = data.end;
    v.truncated = data.truncated;
    renderViewerContent();
  } catch (err) {
    toast(err.message || t('files.loadFailed'), { type: 'error' });
  }
}

// A touched file that's on screen refreshes quietly, debounced — an agent
// making several quick edits shouldn't strobe the viewer.
let _reloadTimer = null;
function maybeReloadViewing(touchedPath) {
  const v = state.viewing;
  if (!v) return;
  if (!(v.path === touchedPath || touchedPath.endsWith(`/${v.path}`))) return;
  clearTimeout(_reloadTimer);
  _reloadTimer = setTimeout(() => {
    if (state.viewing?.path === v.path) openFile(v.path, { silent: true });
  }, 400);
}

// ---- Init -----------------------------------------------------------------------
export function initFiles() {
  els.panel = document.getElementById('files-panel');
  els.btn = document.getElementById('files-btn');
  if (!els.panel || !els.btn) return;
  els.tabRecent = document.getElementById('files-tab-recent');
  els.tabBrowse = document.getElementById('files-tab-browse');
  els.refresh = document.getElementById('files-refresh');
  els.close = document.getElementById('files-close');
  els.crumbs = document.getElementById('files-crumbs');
  els.list = document.getElementById('files-list');
  els.viewer = document.getElementById('files-viewer');
  els.viewerName = document.getElementById('files-viewer-name');
  els.viewerBody = document.getElementById('files-viewer-body');
  els.filter = document.getElementById('files-filter');
  els.wrapToggle = document.getElementById('files-wrap-toggle');
  els.mdToggle = document.getElementById('files-md-toggle');
  els.copyPath = document.getElementById('files-copy-path');
  els.download = document.getElementById('files-download');
  els.viewerClose = document.getElementById('files-viewer-close');
  els.resizer = document.getElementById('files-resizer');
  els.split = document.getElementById('files-split');

  els.refresh.innerHTML = icon('refresh-cw', { size: 15 });
  els.close.innerHTML = icon('x', { size: 16 });
  els.copyPath.innerHTML = icon('copy', { size: 14 });
  els.download.innerHTML = icon('download', { size: 14 });
  els.viewerClose.innerHTML = icon('x', { size: 15 });

  if (els.resizer) initResizer();
  if (els.split) initSplit();

  els.btn.addEventListener('click', () => setOpen(!state.open));
  els.close.addEventListener('click', () => setOpen(false));
  els.refresh.addEventListener('click', refresh);
  els.tabRecent.addEventListener('click', () => setMode('recent'));
  els.tabBrowse.addEventListener('click', () => setMode('browse'));
  els.viewerClose.addEventListener('click', closeViewer);
  els.filter?.addEventListener('input', () => {
    state.filter = els.filter.value.trim();
    renderList();
  });
  if (els.wrapToggle) {
    syncWrapButton();
    els.wrapToggle.addEventListener('click', () => {
      state.wrap = !state.wrap;
      localStorage.setItem('lovia-files-wrap', state.wrap ? '1' : '0');
      syncWrapButton();
      renderViewerContent();
    });
  }
  els.mdToggle.addEventListener('click', () => {
    if (!state.viewing) return;
    state.viewing.raw = !state.viewing.raw;
    renderViewerContent();
  });
  els.copyPath.addEventListener('click', async () => {
    if (!state.viewing) return;
    if (await copyToClipboard(state.viewing.path)) toast(t('toast.pathCopied'));
  });
  els.viewerBody.addEventListener('click', (e) => {
    const a = e.target.closest('a[href]');
    if (!a || !els.viewerBody.contains(a)) return;
    const href = a.getAttribute('href') || '';
    e.preventDefault();
    if (/^[a-z][a-z0-9+.-]*:/i.test(href)) {
      if (/^https?:/i.test(href)) window.open(href, '_blank', 'noopener');
      return; // other schemes (mailto: etc.) — ignore inside the viewer
    }
    if (href.startsWith('#')) return;
    followViewerLink(href);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !state.open) return;
    if (state.viewing) closeViewer();
    else if (window.matchMedia('(max-width: 720px)').matches) setOpen(false);
  });

  store.on('agents-loaded', updateVisibility);
  store.on('agent-changed', () => {
    state.touched.clear();
    state.revs.clear();
    state.browsePath = '';
    state.filter = '';
    if (els.filter) els.filter.value = '';
    closeViewer();
    updateVisibility();
    if (state.open) refresh();
  });
  // "touched" (and the edit revisions) are scoped to the chat on screen.
  store.on('session-switched', () => {
    state.touched.clear();
    state.revs.clear();
  });
  store.on('reset-chat-view', () => {
    state.touched.clear();
    state.revs.clear();
  });
  store.on('workspace-file-touched', ({ path }) => {
    state.touched.add(path);
    state.revs.set(path, (state.revs.get(path) || 0) + 1);
    if (state.open) refresh();
    maybeReloadViewing(path);
  });
  store.on('workspace-maybe-stale', () => {
    state.stale = true;
    if (state.open) els.refresh.classList.add('stale');
  });
  // A tool card's "open in Files panel" action (chat.js) — open the panel and
  // jump straight to the file the tool touched.
  store.on('open-workspace-file', async ({ path }) => {
    if (!state.available) {
      toast(t('files.noWorkspace'));
      return;
    }
    if (!state.open) setOpen(true);
    if (!(await openFile(path))) {
      toast(t('files.openFailed'), { type: 'error' });
    }
  });
}
