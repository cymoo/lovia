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
import {
  formatBytes,
  formatTimeSmart,
  highlightIn,
  IMAGE_EXT,
  renderMarkdownInto,
  workspaceImageUrl,
} from './util.js';

// IMAGE_EXT (browser-renderable image previews, mirroring the server's
// PREVIEW_IMAGE_EXT) is shared from util.js — the chat transcript needs it too.
// SVG rides along in the viewer (see workspaceImageUrl) but stays out of the
// set itself: as an attachment it is a file, not something a model can read.
const MD_EXT = new Set(['md', 'markdown']);
const CSV_EXT = new Set(['csv', 'tsv']);
const HTML_EXT = new Set(['html', 'htm']);
const CSV_MAX_ROWS = 500;

const els = {};
const state = {
  available: false, // current agent has a workspace
  open: false,
  mode: 'recent', // 'recent' | 'browse'
  browsePath: '', // '' = workspace root
  entries: [],
  procs: [], // background processes of this chat's workspace session
  touched: new Set(), // paths this chat's write_file/edit_file produced
  // Per-path edit counter — busts the browser's in-page image cache only when
  // the agent actually touched the file. First opens use the bare URL, so the
  // HTTP cache (revalidated via the server's ETag/no-cache) does its job.
  revs: new Map(),
  stale: false, // a shell run may have changed files
  unseen: 0, // live-run writes since the panel was last open (the button badge)
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
  if (state.open) {
    setUnseen(0); // the badge's job is done — the user is looking
    refresh();
    refreshProcs();
  } else {
    clearTimeout(_procsTimer); // no polling behind a closed panel
    _procsTimer = null;
  }
}

// "n files written since you last looked" — a small count on the Files button.
// Counts only live-run writes: history replay re-emits every past touch on
// each chat open, which would inflate a naive counter.
function setUnseen(n) {
  state.unseen = n;
  let badge = els.btn?.querySelector('.files-badge');
  if (!n) {
    badge?.remove();
    return;
  }
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'files-badge';
    badge.setAttribute('aria-hidden', 'true');
    els.btn?.appendChild(badge);
  }
  badge.textContent = n > 9 ? '9+' : String(n);
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

// ---- Background processes ---------------------------------------------------
// A strip above the file list: what this chat's workspace session still has
// running (dev servers, watchers), with a kill button — "stop the server"
// must not require asking the model. Server-side these live for the chat
// (lovia/web/workspaces.py), so the strip is the user's only window into
// them between runs. Polls only while something is running and the panel is
// open; refetches ride the same signals the file list uses.
let _procsTimer = null;

function refreshProcs() {
  clearTimeout(_procsTimer);
  _procsTimer = null;
  if (!state.open || !store.sessionId) {
    state.procs = [];
    renderProcs();
    return;
  }
  const sid = store.sessionId;
  api
    .sessionProcesses(sid)
    .then((procs) => {
      // The chat (or agent) may have changed while the request was in flight.
      if (!state.open || store.sessionId !== sid) return;
      state.procs = procs;
      renderProcs();
    })
    .catch(() => {}); // transient (e.g. mid-restart) — keep the last render
}

function renderProcs() {
  if (!els.procs) return;
  clearTimeout(_procsTimer);
  _procsTimer = null;
  const procs = state.procs;
  els.procs.classList.toggle('hidden', !procs.length);
  if (!procs.length) {
    els.procs.replaceChildren();
    return;
  }
  const frag = document.createDocumentFragment();
  const running = procs.filter((p) => p.status === 'running').length;
  const head = document.createElement('div');
  head.className = 'files-procs-head';
  head.textContent = t('procs.title');
  if (running) {
    const count = document.createElement('span');
    count.className = 'files-procs-count';
    count.textContent = String(running);
    head.appendChild(count);
  }
  frag.appendChild(head);
  for (const p of procs) frag.appendChild(procRow(p));
  els.procs.replaceChildren(frag);
  // Live processes exit on their own schedule — poll while any are running.
  if (running && state.open) _procsTimer = setTimeout(refreshProcs, 8000);
}

function procRow(p) {
  const row = document.createElement('div');
  row.className = 'proc-row';

  const dot = document.createElement('span');
  dot.className = `proc-dot ${p.status}`;

  const main = document.createElement('span');
  main.className = 'proc-main';
  const cmd = document.createElement('span');
  cmd.className = 'proc-cmd';
  cmd.textContent = p.command;
  const meta = document.createElement('span');
  meta.className = 'proc-meta';
  meta.textContent =
    p.status === 'running'
      ? t('procs.running')
      : p.status === 'killed'
        ? t('procs.killedStatus')
        : t('procs.exited', { code: p.exit_code == null ? '?' : p.exit_code });
  main.append(cmd, meta);
  row.append(dot, main);
  row.title = p.command;

  if (p.status === 'running') {
    const kill = document.createElement('button');
    kill.type = 'button';
    kill.className = 'btn-icon proc-kill';
    kill.innerHTML = icon('x', { size: 14 });
    kill.title = t('procs.kill');
    kill.setAttribute('aria-label', t('procs.kill'));
    kill.addEventListener('click', async () => {
      // Capture the id: if the user switches chats while the kill is in
      // flight, the response is the OLD chat's list — don't paint it onto
      // the new one (the switch handler already refetched for it).
      const sid = store.sessionId;
      kill.disabled = true;
      try {
        const procs = await api.killProcess(sid, p.process_id);
        if (store.sessionId === sid) {
          state.procs = procs;
          renderProcs();
          // The process may have written files right up to its death.
          store.emit('workspace-maybe-stale');
        }
        toast(t('procs.killed'));
      } catch (err) {
        kill.disabled = false;
        toast(err.message || t('procs.killFailed'), { type: 'error' });
      }
    });
    row.appendChild(kill);
  }
  return row;
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
  if (IMAGE_EXT.has(e) || e === 'svg') return icon('image', { size: 15 });
  if (HTML_EXT.has(e)) return icon('code', { size: 15 });
  if (MD_EXT.has(e) || e === 'txt' || e === 'pdf') return icon('file-text', { size: 15 });
  return icon('file', { size: 15 });
}

function rowFor(entry) {
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
  return row;
}

function groupLabel(text) {
  const div = document.createElement('div');
  div.className = 'files-group-label';
  div.textContent = text;
  return div;
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
  // Recent leads with what THIS chat produced — the panel's whole reason to
  // exist for most visits. The split disappears when it wouldn't inform
  // (nothing touched, or everything touched).
  const touched =
    state.mode === 'recent'
      ? entries.filter((e) => !e.is_dir && isTouched(e.path))
      : [];
  if (touched.length && touched.length < entries.length) {
    const inTouched = new Set(touched);
    frag.appendChild(groupLabel(t('files.thisChat')));
    for (const entry of touched) frag.appendChild(rowFor(entry));
    frag.appendChild(groupLabel(t('files.otherFiles')));
    for (const entry of entries) {
      if (!inTouched.has(entry)) frag.appendChild(rowFor(entry));
    }
  } else {
    for (const entry of entries) frag.appendChild(rowFor(entry));
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

// The panel reads through a session locked to the workspace root (see
// api/workspace.py): a 403 is that boundary, not a failure — say so, instead of
// echoing the server's bare "path not readable". Everything the agent may read
// after asking (a skills dir under ~, say) lands here.
function viewerErrorText(err) {
  if (err?.status === 403) return t('files.outsideWorkspace');
  return err?.message || t('files.readFailed');
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
  // No language class → highlightIn skips it. Paginated text never gets this
  // big, but the HTML source view arrives unpaginated via /raw.
  if (content.length <= 200_000 && e && /^[a-z0-9]+$/.test(e)) code.className = `language-${e}`;
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
  // The name truncates from the left (direction: rtl) so the filename tail
  // stays visible — which reorders a path's leading punctuation to the far end
  // ("~/.agents/…/SKILL.md" read as "agents/…/SKILL.md./~"). A bidi isolate
  // keeps the path itself in logical order inside the right-to-left box.
  const label = document.createElement('bdi');
  label.textContent = path;
  els.viewerName.replaceChildren(label);
  els.viewerName.title = path;
  els.download.href = api.workspaceRawUrl({ agent: store.agent, path, download: true });
  els.mdToggle.classList.add('hidden');
  els.wrapToggle?.classList.add('hidden'); // renderViewerContent re-shows for text

  const e = ext(path);
  const kind = IMAGE_EXT.has(e) || e === 'svg'
    ? 'image'
    : e === 'pdf'
      ? 'pdf'
      : HTML_EXT.has(e)
        ? 'html'
        : MD_EXT.has(e)
          ? 'md'
          : CSV_EXT.has(e)
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
    // SVG included: workspaceImageUrl asks for the form that renders in an
    // <img> and downloads on navigation (util.js) — script-inert either way.
    img.src = workspaceImageUrl(store.agent, path) + (rev ? `&v=${rev}` : '');
    els.viewerBody.replaceChildren(img);
    return true;
  }

  if (kind === 'pdf') {
    // The browser's PDF viewer renders in its own sandbox; /raw serves PDFs
    // inline for exactly this embed (older servers answer 415 — the header's
    // download link still works).
    const embed = document.createElement('embed');
    embed.className = 'files-frame';
    embed.type = 'application/pdf';
    const rev = revOf(path);
    embed.src =
      api.workspaceRawUrl({ agent: store.agent, path }) + (rev ? `&v=${rev}` : '');
    els.viewerBody.replaceChildren(embed);
    return true;
  }

  if (kind === 'html') {
    // The whole document, unpaginated — the sandboxed preview needs it in one
    // piece (fetch ignores the download Content-Disposition).
    let text;
    try {
      const res = await fetch(
        api.workspaceRawUrl({ agent: store.agent, path, download: true }),
      );
      if (!res.ok) {
        // Carry the status so a refusal reads as the boundary it is.
        throw Object.assign(new Error(`${res.status} ${res.statusText}`), {
          status: res.status,
        });
      }
      text = await res.text();
    } catch (err) {
      els.viewerBody.replaceChildren(viewerNote(viewerErrorText(err)));
      return false;
    }
    if (state.viewing?.path !== path) return true;
    state.viewing.content = text;
    renderViewerContent();
    return true;
  }

  let data;
  try {
    data = await api.workspaceFile({ agent: store.agent, path });
  } catch (err) {
    els.viewerBody.replaceChildren(viewerNote(viewerErrorText(err)));
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

/** Mirror the panel viewer into the reader dialog.
 *
 * A clone, not a move: the panel keeps its own DOM (and listeners) intact, so
 * closing the reader can never leave it blank. Markdown, CSV, text, and
 * images all clone faithfully; the one interactive control inside — "load
 * more" — is delegated back to the panel above.
 */
function syncModal() {
  const copy = /** @type {HTMLElement} */ (els.viewerBody.cloneNode(true));
  copy.removeAttribute('id');
  els.modalBody.replaceChildren(copy);
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
  const textual =
    v.kind === 'text' ||
    v.kind === 'csv' ||
    ((v.kind === 'md' || v.kind === 'html') && v.raw);
  els.wrapToggle.classList.toggle('hidden', !textual);
  // Copy only means something when there is text content to copy.
  els.copyContent.classList.toggle('hidden', typeof v.content !== 'string');

  if (v.truncated) {
    const more = document.createElement('button');
    more.type = 'button';
    // The class marks it for the reader modal, whose cloned copy delegates
    // back to this same loadMore (a clone carries no listeners of its own).
    more.className = 'btn btn-ghost btn-sm files-load-more';
    more.textContent = t('files.loadMore');
    more.addEventListener('click', loadMore);
    els.viewerBody.appendChild(
      viewerNote(t('files.showingLines', { end: v.end, total: v.totalLines }), more),
    );
  }

  if (v.kind === 'md' || v.kind === 'html') {
    els.mdToggle.classList.remove('hidden');
    els.mdToggle.innerHTML = icon(v.raw ? 'file-text' : 'code', { size: 15 });
    els.mdToggle.title = v.raw ? t('files.rendered') : t('files.raw');
  }
  if (v.kind === 'md' && !v.raw) {
    const { turn, body } = bodyWrapper();
    // Images resolve like the links below: against the document's own
    // directory first, then the workspace root.
    renderMarkdownInto(body, v.content, { agent: store.agent, base: dirname(v.path) });
    highlightIn(body);
    els.viewerBody.appendChild(turn);
    return;
  }
  if (v.kind === 'html' && !v.raw) {
    const frame = document.createElement('iframe');
    frame.className = 'files-frame files-frame--html';
    // allow-scripts WITHOUT allow-same-origin: the document runs in an opaque
    // origin — its scripts can't touch the parent DOM, the token cookie, or
    // localStorage. That's what makes rendering agent-authored HTML safe here.
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.setAttribute('title', v.name);
    frame.srcdoc = v.content;
    els.viewerBody.appendChild(frame);
    return;
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

// ---- Upload into the workspace ---------------------------------------------
// The panel is where "put a file where the agent can reach it" naturally
// lives; the composer's attach flow stays the path for "send WITH the next
// message". Same endpoint (uploads land under uploads/), same allowlist.
async function uploadToWorkspace(files) {
  if (!state.available || !files.length) return;
  let ok = 0;
  for (const file of files) {
    try {
      await api.uploadFile(file, { agent: store.agent });
      ok += 1;
    } catch {
      toast(t('composer.uploadFailed', { name: file.name }), { type: 'error' });
    }
  }
  if (ok) {
    toast(t('files.uploaded', { n: ok }));
    if (state.mode !== 'recent') setMode('recent'); // that's where they surface
    else refresh();
  }
}

function initUpload() {
  if (!els.upload) return;
  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = true;
  input.style.display = 'none';
  document.body.appendChild(input);
  input.addEventListener('change', () => {
    if (input.files?.length) uploadToWorkspace([...input.files]);
    input.value = ''; // let the same file be picked again
  });
  els.upload.addEventListener('click', () => input.click());

  els.panel.addEventListener('dragover', (e) => {
    if (!state.available) return;
    if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
    e.preventDefault();
    els.panel.classList.add('drag-over');
  });
  els.panel.addEventListener('dragleave', (e) => {
    if (!els.panel.contains(/** @type {Node} */ (e.relatedTarget))) {
      els.panel.classList.remove('drag-over');
    }
  });
  els.panel.addEventListener('drop', (e) => {
    els.panel.classList.remove('drag-over');
    if (!state.available) return;
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) {
      e.preventDefault();
      uploadToWorkspace(files);
    }
  });
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
/** Wire up the Files panel: open/close, Recent/Browse tabs, the viewer, and workspace-change listeners. */
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
  els.procs = document.getElementById('files-procs');
  els.viewer = document.getElementById('files-viewer');
  els.viewerName = document.getElementById('files-viewer-name');
  els.viewerBody = document.getElementById('files-viewer-body');
  els.filter = document.getElementById('files-filter');
  els.wrapToggle = document.getElementById('files-wrap-toggle');
  els.mdToggle = document.getElementById('files-md-toggle');
  els.attach = document.getElementById('files-attach');
  els.copyContent = document.getElementById('files-copy');
  els.fullscreen = document.getElementById('files-fullscreen');
  els.modal = document.getElementById('file-modal');
  els.modalName = document.getElementById('file-modal-name');
  els.modalBody = document.getElementById('file-modal-body');
  els.modalClose = document.getElementById('file-modal-close');
  els.download = document.getElementById('files-download');
  els.viewerClose = document.getElementById('files-viewer-close');
  els.resizer = document.getElementById('files-resizer');
  els.split = document.getElementById('files-split');
  els.upload = document.getElementById('files-upload');

  els.refresh.innerHTML = icon('refresh-cw', { size: 15 });
  els.close.innerHTML = icon('x', { size: 16 });
  els.attach.innerHTML = icon('paperclip', { size: 14 });
  els.copyContent.innerHTML = icon('copy', { size: 14 });
  els.copyContent.title = t('files.copyContent');
  els.fullscreen.innerHTML = icon('maximize-2', { size: 15 });
  els.fullscreen.title = t('files.fullscreen');
  els.modalClose.innerHTML = icon('x', { size: 16 });
  els.download.innerHTML = icon('download', { size: 14 });
  els.viewerClose.innerHTML = icon('x', { size: 15 });
  if (els.upload) els.upload.innerHTML = icon('upload', { size: 15 });

  if (els.resizer) initResizer();
  if (els.split) initSplit();
  initUpload();

  els.btn.addEventListener('click', () => setOpen(!state.open));
  els.close.addEventListener('click', () => setOpen(false));
  els.refresh.addEventListener('click', () => {
    refresh();
    refreshProcs();
  });
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
  els.copyContent.addEventListener('click', async () => {
    const content = state.viewing?.content;
    if (typeof content !== 'string') return;
    if (await copyToClipboard(content)) toast(t('toast.contentCopied'));
  });
  els.fullscreen.addEventListener('click', () => {
    const v = state.viewing;
    if (!v) return;
    els.modalName.textContent = v.name || v.path;
    syncModal();
    els.modal.showModal();
  });
  // The reader holds a clone, and clones carry no listeners — delegate its two
  // live controls back to the panel, then re-mirror. "Load more" extends the
  // page; a workspace link keeps the reader reading, same as the viewer below
  // (a modified click still follows the href into a tab of its own).
  els.modalBody.addEventListener('click', async (e) => {
    const target = e.target instanceof Element ? e.target : null;
    if (target?.closest('.files-load-more')) {
      await loadMore();
      syncModal();
      return;
    }
    const a = target?.closest('a[data-ws-path]');
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    await followViewerLink(a.dataset.wsPath);
    syncModal();
  });
  els.modalClose.addEventListener('click', () => els.modal.close());
  els.modal.addEventListener('click', (e) => {
    if (e.target === els.modal) els.modal.close(); // backdrop click
  });
  els.modal.addEventListener('close', () => els.modalBody.replaceChildren());
  // The reverse of the tool card's "open in Files panel": put the viewed file
  // on the next message. chat.js owns the composer tray and answers.
  els.attach.addEventListener('click', () => {
    const v = state.viewing;
    if (!v) return;
    store.emit('attach-workspace-file', {
      path: v.path,
      name: v.name,
      kind: IMAGE_EXT.has(ext(v.path)) ? 'image' : 'file',
    });
  });
  els.viewerBody.addEventListener('click', (e) => {
    const a = e.target.closest('a[href]');
    if (!a || !els.viewerBody.contains(a)) return;
    // `wsPath` is the reference as authored: the renderer points the href at
    // the raw endpoint (which is what a new tab or the reader dialog needs),
    // but in the panel a workspace file opens in the viewer instead.
    const href = a.dataset.wsPath || a.getAttribute('href') || '';
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
    // The reader dialog owns Escape while it is up (it closes itself) —
    // otherwise one keypress would close the viewer underneath it too.
    if (els.modal?.open) return;
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
    setUnseen(0);
    closeViewer();
    updateVisibility();
    if (state.open) {
      refresh();
      refreshProcs();
    }
  });
  // "touched" (and the edit revisions) are scoped to the chat on screen —
  // and so are background processes.
  store.on('session-switched', () => {
    state.touched.clear();
    state.revs.clear();
    setUnseen(0);
    state.procs = [];
    renderProcs();
    refreshProcs();
  });
  store.on('reset-chat-view', () => {
    state.touched.clear();
    state.revs.clear();
    setUnseen(0);
    state.procs = [];
    renderProcs();
    refreshProcs();
  });
  store.on('workspace-file-touched', ({ path }) => {
    state.touched.add(path);
    state.revs.set(path, (state.revs.get(path) || 0) + 1);
    // Badge only live-run writes — replayed history re-emits old touches.
    if (!state.open && store.streaming) setUnseen(state.unseen + 1);
    if (state.open) refresh();
    maybeReloadViewing(path);
  });
  store.on('workspace-maybe-stale', () => {
    state.stale = true;
    if (state.open) els.refresh.classList.add('stale');
    // The same tool completions that can change files (shell, output polls,
    // kills) are exactly the moments the process list can change.
    if (state.open) refreshProcs();
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
