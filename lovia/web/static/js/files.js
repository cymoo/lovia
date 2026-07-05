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
import { store } from './store.js';
import { api } from './api.js';
import { copyToClipboard } from './ui.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { formatBytes, formatTimeSmart, highlightIn, renderMarkdown } from './util.js';

const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'avif', 'bmp', 'ico']);
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
  stale: false, // a shell run may have changed files
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

// ---- Panel open/close -----------------------------------------------------
// `persist: false` is for forced closes (agent without a workspace) — they
// must not overwrite the user's remembered open/closed preference.
function setOpen(open, { persist = true } = {}) {
  state.open = open && state.available;
  els.panel.classList.toggle('open', state.open);
  els.btn?.setAttribute('aria-expanded', String(state.open));
  if (persist) localStorage.setItem('lovia-files-open', state.open ? '1' : '0');
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
    els.list.replaceChildren(emptyNode(err.message || 'Couldn’t load files'));
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
  if (!state.entries.length) {
    frag.appendChild(
      emptyNode(state.mode === 'recent' ? 'No files yet.' : 'Empty directory.'),
    );
  }
  for (const entry of state.entries) {
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
    if (entry.is_dir) bits.push('folder');
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
  els.viewer.classList.remove('hidden');
  els.viewerName.textContent = path;
  els.viewerName.title = path;
  els.download.href = api.workspaceRawUrl({ agent: store.agent, path, download: true });
  els.mdToggle.classList.add('hidden');

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
    img.src = api.workspaceRawUrl({ agent: store.agent, path }) + `&t=${Date.now()}`;
    els.viewerBody.replaceChildren(img);
    return true;
  }

  let data;
  try {
    data = await api.workspaceFile({ agent: store.agent, path });
  } catch (err) {
    els.viewerBody.replaceChildren(viewerNote(err.message || 'Couldn’t read file'));
    return false;
  }
  if (state.viewing?.path !== path) return true; // user opened something else meanwhile

  if (data.binary) {
    const dl = document.createElement('a');
    dl.className = 'btn btn-ghost btn-sm';
    dl.textContent = 'Download';
    dl.href = api.workspaceRawUrl({ agent: store.agent, path, download: true });
    dl.setAttribute('download', '');
    els.viewerBody.replaceChildren(
      viewerNote('Binary file — no preview.', dl),
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

function renderViewerContent() {
  const v = state.viewing;
  if (!v) return;
  els.viewerBody.replaceChildren();

  if (v.truncated) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'btn btn-ghost btn-sm';
    more.textContent = 'Load more';
    more.addEventListener('click', loadMore);
    els.viewerBody.appendChild(
      viewerNote(`Showing lines 1–${v.end} of ${v.totalLines}.`, more),
    );
  }

  if (v.kind === 'md') {
    els.mdToggle.classList.remove('hidden');
    els.mdToggle.innerHTML = icon(v.raw ? 'file-text' : 'code', { size: 15 });
    els.mdToggle.title = v.raw ? 'Show rendered' : 'Show raw markdown';
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
    toast(err.message || 'Couldn’t load more', { type: 'error' });
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
  els.mdToggle = document.getElementById('files-md-toggle');
  els.copyPath = document.getElementById('files-copy-path');
  els.download = document.getElementById('files-download');
  els.viewerClose = document.getElementById('files-viewer-close');

  els.refresh.innerHTML = icon('refresh-cw', { size: 15 });
  els.close.innerHTML = icon('x', { size: 16 });
  els.copyPath.innerHTML = icon('copy', { size: 14 });
  els.download.innerHTML = icon('download', { size: 14 });
  els.viewerClose.innerHTML = icon('x', { size: 15 });

  els.btn.addEventListener('click', () => setOpen(!state.open));
  els.close.addEventListener('click', () => setOpen(false));
  els.refresh.addEventListener('click', refresh);
  els.tabRecent.addEventListener('click', () => setMode('recent'));
  els.tabBrowse.addEventListener('click', () => setMode('browse'));
  els.viewerClose.addEventListener('click', closeViewer);
  els.mdToggle.addEventListener('click', () => {
    if (!state.viewing) return;
    state.viewing.raw = !state.viewing.raw;
    renderViewerContent();
  });
  els.copyPath.addEventListener('click', async () => {
    if (!state.viewing) return;
    if (await copyToClipboard(state.viewing.path)) toast('Path copied');
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
    state.browsePath = '';
    closeViewer();
    updateVisibility();
    if (state.open) refresh();
  });
  // "touched" is scoped to the chat on screen.
  store.on('session-switched', () => state.touched.clear());
  store.on('reset-chat-view', () => state.touched.clear());
  store.on('workspace-file-touched', ({ path }) => {
    state.touched.add(path);
    if (state.open) refresh();
    maybeReloadViewing(path);
  });
  store.on('workspace-maybe-stale', () => {
    state.stale = true;
    if (state.open) els.refresh.classList.add('stale');
  });
}
