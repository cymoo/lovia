// Files panel — a read-only window into the agent's workspace.
//
// lovia is a personal assistant, so the panel leads with "Changes": the files
// this chat wrote or brought in, newest first, then — in a git work tree — the
// other uncommitted changes, folded. "Browse" (breadcrumb + one directory
// level) is the secondary view. The viewer renders documents as documents:
// markdown rich by default, images inline, CSV as a table; code gets
// highlighting; a changed tracked file also offers its diff against HEAD.
//
// Wiring: chat.js emits `workspace-file-touched` for successful
// write_file/edit_file calls and for a sent message's attachments (live and
// replayed history), and `workspace-maybe-stale` after a shell run; this
// module owns everything else.
import { t } from './i18n.js';
import { store } from './store.js';
import { api, apiError } from './api.js';
import {
  addCodeBlockControls,
  copyCodeBlock,
  copyToClipboard,
  menuOpen,
  openImageLightbox,
  openMenu,
  setSidebarAutoCollapsed,
  widthDivider,
} from './ui.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { isRemote, scopeRoot, workspaceName } from './workspaces.js';
import { sortRows } from './tables.js';
import { renderMermaid } from './diagrams.js';
import { sandboxFrame } from './sandbox.js';
import {
  formatBytes,
  formatTimeSmart,
  highlightIn,
  IMAGE_EXT,
  normalizeRel,
  renderMarkdownInto,
  uploadFailedText,
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
  mode: 'changes', // 'changes' | 'browse'
  browsePath: '', // '' = workspace root
  entries: [],
  // The last change list, by path — what the viewer asks whether a file has
  // a diff to show (also while browsing). `git` says whether there is one at all.
  changes: new Map(),
  git: false,
  truncated: false, // git listed more than the list shows
  othersOpen: false, // "other uncommitted changes" unfolded
  procs: [], // background processes of this chat's workspace session
  touched: new Set(), // paths this chat wrote, or attached to a message
  // Per-path edit counter — busts the browser's in-page image cache only when
  // the agent actually touched the file. First opens use the bare URL, so the
  // HTTP cache (revalidated via the server's ETag/no-cache) does its job.
  revs: new Map(),
  stale: false, // a shell run may have changed files
  // Deliverable paths written by the live run since the panel was last open
  // (the button badge). A set: three edits to one file are one new file.
  unseen: new Set(),
  failed: false, // the last listing failed: the list shows why, and a retry
  filter: '', // case-insensitive substring over listed paths
  wrap: localStorage.getItem('lovia-files-wrap') !== '0', // wrap long lines
  viewing: null, // { path, kind, raw, name, end, totalLines, truncated, view, diff }
  listScroll: 0, // the list's scroll when a file covered it (narrow panel)
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
// double-click resets). The width lives in a `--lv-files-w` CSS var and persists
// per browser; unset, the stylesheet default applies.
const WIDTH_KEY = 'lovia-files-w';
const MIN_W = 300;
const RESERVED_W = 520; // keep at least this much viewport for the chat column

const isPhone = () => window.matchMedia('(max-width: 720px)').matches;
const clampW = (w) =>
  Math.min(Math.max(Math.round(w), MIN_W), Math.max(MIN_W, window.innerWidth - RESERVED_W));
const panelWidth = () => els.panel.getBoundingClientRect().width;

function applyWidth(px) {
  if (px == null) document.documentElement.style.removeProperty('--lv-files-w');
  else document.documentElement.style.setProperty('--lv-files-w', `${clampW(px)}px`);
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
      getComputedStyle(document.documentElement).getPropertyValue('--lv-sidebar-w'),
      10,
    ) || 272;
  setSidebarAutoCollapsed(window.innerWidth < sidebarW + panelWidth() + 760);
}

function initResizer() {
  const saved = Number(localStorage.getItem(WIDTH_KEY));
  if (saved) applyWidth(saved);

  widthDivider(els.resizer, {
    width: panelWidth,
    apply: applyWidth,
    grow: -1, // the panel sits right: dragging left widens it
    busyClass: 'lv-files-resizing',
    done: (reset) => {
      if (reset) localStorage.removeItem(WIDTH_KEY);
      else localStorage.setItem(WIDTH_KEY, String(Math.round(panelWidth())));
      claimSpace();
    },
  });
  store.on('sidebar-resized', claimSpace);

  // A window resize can re-tighten (or free) the space the open panel needs.
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(claimSpace, 120);
  });
}

// ---- List / preview layout --------------------------------------------------
// An open file takes the panel (`lv-viewing`): on a narrow panel the list
// steps aside and the preview's crumbs lead back; once the panel is dragged
// wide enough (`lv-files-wide`), the list stays beside the preview instead.
// Width is the panel's own, so a ResizeObserver decides — not the viewport.
const WIDE_W = 720;
const isWide = () => els.panel.classList.contains('lv-files-wide');

function initLayout() {
  new ResizeObserver(() => {
    els.panel.classList.toggle('lv-files-wide', els.panel.clientWidth >= WIDE_W);
  }).observe(els.panel);
}

// ---- Panel open/close -----------------------------------------------------
// `persist: false` is for forced closes (agent without a workspace) — they
// must not overwrite the user's remembered open/closed preference.
function setOpen(open, { persist = true } = {}) {
  state.open = open && state.available;
  els.panel.classList.toggle('lv-open', state.open);
  els.btn?.setAttribute('aria-expanded', String(state.open));
  if (persist) localStorage.setItem('lovia-files-open', state.open ? '1' : '0');
  claimSpace();
  if (state.open) {
    clearUnseen(); // the badge's job is done — the user is looking
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
function renderBadge() {
  const n = state.unseen.size;
  let badge = els.btn?.querySelector('.lv-files-badge');
  if (!n) {
    badge?.remove();
    return;
  }
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'lv-files-badge';
    badge.setAttribute('aria-hidden', 'true');
    els.btn?.appendChild(badge);
  }
  badge.textContent = n > 9 ? '9+' : String(n);
}

function clearUnseen() {
  state.unseen.clear();
  renderBadge();
}

/**
 * One key per file for `touched` / `revs` / `unseen`: tool args spell the
 * same path as `report.md`, `./report.md` or `a/../report.md`. Absolute
 * paths stay as written — the page never learns the root to relativize them.
 */
function touchKey(path) {
  return path.startsWith('/') ? path : normalizeRel(path);
}

/** True for a path under the agent's scratch dir (root-relative, like the
 * server's change list). `null` scratch dir means nothing is scratch. */
function isScratch(key, dir) {
  return !!dir && (key === dir || key.startsWith(`${dir}/`));
}

// The scratch dir comes from /api/workspace, per agent. Classification awaits
// this promise rather than reading a field, so a write that lands before the
// answer does is still judged by it — a failed request settles to null.
let _info = null; // { agent, promise: Promise<string | null> }
let _rootName = /** @type {string | null} */ (null); // the workspace's display name
function scratchDirFor(agent) {
  if (_info?.agent !== agent) {
    _rootName = null;
    const promise = api.workspaceInfo({ agent }).then(
      (info) => {
        _rootName = info.name || null;
        renderCrumbs(); // the root crumb waits for the name
        return info.scratch_dir || null;
      },
      () => null,
    );
    _info = { agent, promise };
  }
  return _info.promise;
}

function updateVisibility() {
  const agent = store.agents.find((a) => a.name === store.agent);
  state.available = !!agent?.workspace;
  els.btn?.classList.toggle('lv-hidden', !state.available);
  if (state.available) scratchDirFor(store.agent); // warm it for the first write
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
// The listing is asked for in two ways. `refresh()` is the user's own move (a
// tab, a crumb, another chat's folder): it asks at once, and whatever was
// still on its way for the place just left is dropped when it lands.
// `queueRefresh()` is for what happens to the files meanwhile — every write
// the chat makes, and every past one a history replay re-emits on opening a
// chat: a burst of those asks once, and never while an answer is pending
// (each ask stats every touched path and runs git, on another machine for a
// remote folder).
const MAX_TOUCHED = 500; // ChangesRequest.paths' cap (schemas.py)
let _refreshSeq = 0; // the newest request; an older one's answer is stale
let _refreshing = false; // the newest request is still out
let _refreshAgain = false; // something changed while it was
let _refreshTimer = null;
let _slowTimer = null;

function queueRefresh() {
  if (!state.open || _refreshTimer) return;
  if (_refreshing) {
    _refreshAgain = true;
    return;
  }
  _refreshTimer = setTimeout(refresh, 150);
}

// The list repaints from what is already here (another chat's files drop
// out) without waiting for the next listing; a burst paints once.
let _renderQueued = false;
function queueRender() {
  if (_renderQueued) return;
  _renderQueued = true;
  queueMicrotask(() => {
    _renderQueued = false;
    if (state.open) renderList();
  });
}

async function refresh() {
  clearTimeout(_refreshTimer);
  _refreshTimer = null;
  _refreshAgain = false;
  if (!state.open) return;
  const seq = ++_refreshSeq;
  _refreshing = true;
  state.stale = false;
  els.refresh.classList.remove('lv-stale');
  els.refresh.classList.add('lv-loading');
  // An empty list says it is on its way only once the wait is felt (a
  // folder on another machine): a local one answers before the eye asks.
  clearTimeout(_slowTimer);
  _slowTimer = setTimeout(() => {
    if (!state.entries.length) els.list.replaceChildren(emptyNode(t('files.loading')));
  }, 300);
  let entries = null;
  let changes = null;
  let failure = null;
  try {
    if (state.mode === 'changes') {
      // The server takes 500 at most: the most recently touched, in a chat that long.
      const paths = [...state.touched].slice(-MAX_TOUCHED);
      changes = await api.workspaceChanges({ agent: store.agent, paths });
      entries = changes.files;
    } else {
      entries = await api.workspaceFiles({ agent: store.agent, path: state.browsePath || '.' });
    }
  } catch (err) {
    failure = err;
  }
  if (seq !== _refreshSeq) return; // a newer request owns the list now
  clearTimeout(_slowTimer);
  _refreshing = false;
  els.refresh.classList.remove('lv-loading');
  if (_refreshAgain) queueRefresh();
  state.failed = !!failure;
  if (failure) {
    state.entries = [];
    const failed = emptyNode(failure.message || t('files.loadFailed'));
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'lv-btn lv-btn-ghost lv-btn-sm lv-files-empty-upload';
    retry.innerHTML = icon('refresh-cw', { size: 14 });
    retry.append(t('files.retry'));
    retry.addEventListener('click', refresh);
    failed.append(retry);
    els.list.replaceChildren(failed);
    return;
  }
  state.entries = entries;
  if (changes) {
    state.git = changes.git;
    state.truncated = changes.truncated;
    state.changes = new Map(changes.files.map((f) => [f.path, f]));
    syncViewerViews(); // a file on screen may just have gained (or lost) a diff
  }
  renderCrumbs();
  renderList();
  void restoreViewing();
}

/** The list has nothing of the place just left to show while the new one loads. */
function clearList() {
  state.entries = [];
  state.changes = new Map();
  state.git = false;
  state.failed = false;
  els.list.replaceChildren();
}

function emptyNode(text) {
  const div = document.createElement('div');
  div.className = 'lv-files-empty';
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
  els.procs.classList.toggle('lv-hidden', !procs.length);
  if (!procs.length) {
    els.procs.replaceChildren();
    return;
  }
  const frag = document.createDocumentFragment();
  const running = procs.filter((p) => p.status === 'running').length;
  const head = document.createElement('div');
  head.className = 'lv-files-procs-head';
  head.textContent = t('procs.title');
  if (running) {
    const count = document.createElement('span');
    count.className = 'lv-files-procs-count';
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
  row.className = 'lv-proc-row';

  const dot = document.createElement('span');
  dot.className = `lv-proc-dot lv-${p.status}`;

  const main = document.createElement('span');
  main.className = 'lv-proc-main';
  const cmd = document.createElement('span');
  cmd.className = 'lv-proc-cmd';
  cmd.textContent = p.command;
  const meta = document.createElement('span');
  meta.className = 'lv-proc-meta';
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
    kill.className = 'lv-btn-icon lv-proc-kill';
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
  // A tab is also the way back to its list from a file covering the panel.
  if (state.viewing && !isWide()) closeViewer();
  state.mode = mode;
  els.tabChanges.classList.toggle('lv-active', mode === 'changes');
  els.tabChanges.setAttribute('aria-selected', String(mode === 'changes'));
  els.tabBrowse.classList.toggle('lv-active', mode === 'browse');
  els.tabBrowse.setAttribute('aria-selected', String(mode === 'browse'));
  els.crumbs.classList.toggle('lv-hidden', mode !== 'browse');
  refresh();
}

/**
 * A crumb row: the workspace (folder glyph, its path on hover), then one crumb
 * per segment of `path`. Every crumb but the last opens Browse at that
 * directory; the last is where you are. Shared by the list's row (`path` = the
 * directory shown) and the preview's header (`path` = the open file).
 * `fold` keeps a deep path to `root / … / parent / last` (the preview header
 * has one line): "…" opens the deepest folder it hides.
 * @param {HTMLElement} into @param {string} path
 * @param {(dir: string) => void} go
 * @param {{ fold?: boolean }} [opts]
 */
function fillCrumbs(into, path, go, { fold = false } = {}) {
  into.replaceChildren();
  const parts = path ? path.split('/') : [];
  // Folded, the middle folders share one crumb; `skip` is how many it hides.
  const skip = fold && parts.length > 3 ? parts.length - 3 : 0;
  const crumb = (label, dir, last) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'lv-files-crumb';
    b.textContent = label;
    if (last) b.disabled = true;
    else b.addEventListener('click', () => go(dir));
    return b;
  };
  // The root names the folder the chat works in (its path on hover), not "~".
  const root = scopeRoot();
  const home = crumb('', '', parts.length === 0);
  home.classList.add('lv-files-crumb-root');
  if (root) home.title = root;
  const homeName = document.createElement('span'); // ellipsizes; the glyph doesn't
  homeName.textContent = root ? workspaceName(root) : _rootName || '~';
  // … behind the glyph the sidebar gives it: a server for one on another machine.
  home.innerHTML = icon(root && isRemote(root) ? 'server' : 'folder', { size: 13 });
  home.appendChild(homeName);
  into.appendChild(home);
  parts.forEach((part, i) => {
    if (i > 0 && i <= skip) return; // inside the fold
    const sep = document.createElement('span');
    sep.className = 'lv-files-crumb-sep';
    sep.textContent = '/';
    into.appendChild(sep);
    if (i === 0 && skip) {
      const hidden = parts.slice(0, skip + 1).join('/');
      const more = crumb('…', hidden, false);
      more.title = hidden;
      into.appendChild(more);
      return;
    }
    into.appendChild(crumb(part, parts.slice(0, i + 1).join('/'), i === parts.length - 1));
  });
}

function renderCrumbs() {
  if (state.mode !== 'browse' || !els.crumbs) return;
  fillCrumbs(els.crumbs, state.browsePath, (dir) => {
    state.browsePath = dir;
    refresh();
  });
}

/**
 * A preview crumb: Browse at `dir`. Narrow, that means leaving the file for
 * the list; wide, the list beside it moves and the file stays open.
 */
function browseTo(dir) {
  state.browsePath = dir;
  state.listScroll = 0; // a different list than the one left behind
  if (!isWide()) closeViewer();
  if (state.mode === 'browse') refresh();
  else setMode('browse');
}

// What a row's icon says the file is — by extension, as file managers do.
const CODE_EXT = new Set([
  'py', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'go', 'rs', 'java', 'kt', 'c', 'h', 'cc',
  'cpp', 'hpp', 'cs', 'rb', 'php', 'swift', 'sh', 'bash', 'zsh', 'sql', 'lua', 'r',
  'scala', 'vue', 'svelte', 'css', 'scss', 'less',
]);
const DATA_EXT = new Set(['json', 'jsonl', 'yaml', 'yml', 'toml', 'ini', 'xml']);

function rowIcon(entry) {
  if (entry.is_dir) return icon('folder', { size: 15 });
  const e = ext(entry.path);
  const name =
    IMAGE_EXT.has(e) || e === 'svg'
      ? 'file-image'
      : HTML_EXT.has(e) || CODE_EXT.has(e)
        ? 'file-code'
        : DATA_EXT.has(e)
          ? 'file-json'
          : CSV_EXT.has(e)
            ? 'file-spreadsheet'
            : MD_EXT.has(e) || e === 'txt' || e === 'pdf'
              ? 'file-text'
              : 'file';
  return icon(name, { size: 15 });
}

/**
 * A row's right-click menu — what you'd otherwise open the file to reach:
 * attach it to the chat, copy its path, download it. A folder (or a file git
 * says is deleted) offers its path.
 * @param {{ path: string, is_dir: boolean, status?: string | null }} entry
 * @param {MouseEvent} e
 * @param {HTMLElement} row
 */
function rowMenu(entry, e, row) {
  e.preventDefault();
  // From the keyboard (Shift+F10, the menu key) there's no pointer: anchor on the row.
  const at = e.clientX || e.clientY ? { x: e.clientX, y: e.clientY } : row;
  const copy = {
    label: t('files.copyPath'),
    icon: icon('copy', { size: 15 }),
    onSelect: async () => {
      if (await copyToClipboard(entry.path)) toast(t('ws.pathCopied'));
    },
  };
  if (entry.is_dir || entry.status === 'deleted') {
    openMenu(at, [copy]);
    return;
  }
  openMenu(at, [
    {
      label: t('files.attach'),
      icon: icon('paperclip', { size: 15 }),
      onSelect: () =>
        store.emit('attach-workspace-file', {
          path: entry.path,
          name: basename(entry.path),
          kind: IMAGE_EXT.has(ext(entry.path)) ? 'image' : 'file',
        }),
    },
    copy,
    {
      label: t('files.download'),
      icon: icon('download', { size: 15 }),
      onSelect: () => {
        const a = document.createElement('a');
        a.href = api.workspaceRawUrl({ agent: store.agent, path: entry.path, download: true });
        a.download = '';
        a.click();
      },
    },
  ]);
}

/** What git says changed about a change-list row: "+12 −3", or "New". */
function changeMark(entry) {
  const mark = document.createElement('span');
  mark.className = 'lv-file-change';
  if (entry.status === 'untracked' || entry.status === 'added') {
    mark.classList.add('lv-new');
    mark.textContent = t('files.newFile');
    return mark;
  }
  for (const [n, sign, cls] of [
    [entry.added, '+', 'lv-add'],
    [entry.removed, '−', 'lv-del'],
  ]) {
    if (!n) continue;
    const span = document.createElement('span');
    span.className = cls;
    span.textContent = `${sign}${n}`;
    mark.appendChild(span);
  }
  return mark.childElementCount ? mark : null;
}

function rowFor(entry) {
  const changes = state.mode === 'changes';
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'lv-file-row';
  // Browsing, a dot marks what this chat wrote; the change list groups it instead.
  if (!changes && !entry.is_dir && isTouched(entry.path)) row.classList.add('lv-touched');
  if (entry.status === 'deleted') row.classList.add('lv-deleted');
  if (state.viewing?.path === entry.path) row.classList.add('lv-active');

  const ic = document.createElement('span');
  ic.className = 'lv-file-row-icon';
  ic.innerHTML = rowIcon(entry);

  const main = document.createElement('span');
  main.className = 'lv-file-row-main';
  const name = document.createElement('span');
  name.className = 'lv-file-row-name';
  name.textContent = basename(entry.path);
  const sub = document.createElement('span');
  sub.className = 'lv-file-row-sub';
  const bits = [];
  if (changes && dirname(entry.path)) bits.push(dirname(entry.path));
  if (entry.old_path) bits.push(t('files.renamedFrom', { path: entry.old_path }));
  // A folder's icon already says what it is; its line keeps just the time.
  if (!entry.is_dir && entry.size != null) bits.push(formatBytes(entry.size));
  if (entry.mtime) bits.push(formatTimeSmart(entry.mtime));
  if (entry.symlink_target) bits.push('→ ' + entry.symlink_target);
  sub.textContent = bits.join(' · ');
  main.append(name, sub);

  row.append(ic, main);
  const mark = changes ? changeMark(entry) : null;
  if (mark) row.appendChild(mark);
  row.title = entry.path;
  row.addEventListener('contextmenu', (e) => rowMenu(entry, e, row));
  row.addEventListener('click', () => {
    if (entry.is_dir) {
      state.mode === 'browse' || setMode('browse');
      state.browsePath = entry.path;
      refresh();
    } else {
      // From the change list a file opens on what changed; browsing, on itself.
      openFile(entry.path, { view: changes ? 'diff' : 'file' });
    }
  });
  return row;
}

function groupLabel(text) {
  const div = document.createElement('div');
  div.className = 'lv-files-group-label';
  div.textContent = text;
  return div;
}

/** "Other uncommitted changes · 7": the folded group's header, which unfolds it. */
function othersToggle(count) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'lv-files-group-label lv-files-group-toggle';
  b.setAttribute('aria-expanded', String(state.othersOpen));
  b.innerHTML = icon('chevron-right', { size: 12 });
  b.append(`${t('files.otherChanges')} · ${count}`);
  b.addEventListener('click', () => {
    state.othersOpen = !state.othersOpen;
    renderList();
  });
  return b;
}

function renderList() {
  if (state.failed) return; // the list is the failure and its retry until one works
  const frag = document.createDocumentFragment();
  const needle = state.filter.toLowerCase();
  const entries = needle
    ? state.entries.filter((e) => e.path.toLowerCase().includes(needle))
    : state.entries;
  if (state.mode === 'browse') {
    if (!entries.length) frag.appendChild(emptyNode(needle ? t('files.noMatch') : t('files.emptyDir')));
    for (const entry of entries) frag.appendChild(rowFor(entry));
    els.list.replaceChildren(frag);
    return;
  }
  // The change list leads with what THIS chat produced — the panel's whole
  // reason to exist for most visits — then, folded, what else git sees.
  const mine = entries.filter((e) => e.touched);
  const others = entries.filter((e) => !e.touched);
  if (!mine.length && !needle) {
    // A chat with no files yet says how they get here, and offers the first way.
    const empty = emptyNode(t('files.noFiles'));
    if (els.upload) {
      const up = document.createElement('button');
      up.type = 'button';
      up.className = 'lv-btn lv-btn-ghost lv-btn-sm lv-files-empty-upload';
      up.innerHTML = icon('upload', { size: 14 });
      up.append(t('files.uploadAction'));
      up.addEventListener('click', () => els.upload?.click());
      empty.append(up);
    }
    frag.appendChild(empty);
  } else if (!entries.length) {
    frag.appendChild(emptyNode(t('files.noMatch')));
  }
  if (mine.length && others.length) frag.appendChild(groupLabel(t('files.thisChat')));
  for (const entry of mine) frag.appendChild(rowFor(entry));
  if (others.length) {
    frag.appendChild(othersToggle(others.length));
    // A filter looks inside the fold too: what it found is shown.
    if (state.othersOpen || needle) {
      for (const entry of others) frag.appendChild(rowFor(entry));
      if (state.truncated && !needle) {
        frag.appendChild(emptyNode(t('files.changesCut', { n: others.length })));
      }
    }
  }
  els.list.replaceChildren(frag);
}

// ---- Viewer -------------------------------------------------------------------
// ---- The open file, across a reload --------------------------------------------
// Each tab remembers the file it had open, per agent and folder
// (sessionStorage), and reopens it once the folder is known — if it still
// exists. Only the user's own close forgets it: a switch of agent or folder
// closes the viewer too, but the file belongs to the scope it was opened in.
const VIEWING_KEY = 'lovia-files-viewing';
const viewingScope = () => `${store.agent}|${scopeRoot() ?? ''}`;
let _restoredScope = /** @type {string | null} */ (null);

/** @param {string | null} path */
function rememberViewing(path) {
  try {
    if (path) sessionStorage.setItem(VIEWING_KEY, JSON.stringify({ scope: viewingScope(), path }));
    else sessionStorage.removeItem(VIEWING_KEY);
  } catch {
    /* storage off: this tab just won't remember */
  }
}

async function restoreViewing() {
  const scope = viewingScope();
  if (state.viewing || _restoredScope === scope) return;
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(VIEWING_KEY) || 'null');
  } catch {
    /* unreadable: nothing to restore */
  }
  if (saved?.scope !== scope || typeof saved.path !== 'string') return;
  _restoredScope = scope; // once per scope: a later refresh mustn't reopen it
  try {
    await api.workspaceFile({ agent: store.agent, path: saved.path }); // still there?
  } catch {
    rememberViewing(null);
    return;
  }
  if (!state.viewing && viewingScope() === scope) openFile(saved.path);
}

/** @param {{ forget?: boolean }} [opts] `forget: false` for a close the user didn't ask for. */
function closeViewer({ forget = true } = {}) {
  if (forget) rememberViewing(null);
  state.viewing = null;
  els.viewer.classList.add('lv-hidden');
  els.panel.classList.remove('lv-viewing');
  els.viewerBody.replaceChildren();
  renderList(); // drop the active highlight
  // A narrow panel hid the list behind the file: back where the user left it.
  // (Wide, the list stayed in view and its scroll is the user's own.)
  if (!isWide()) els.list.scrollTop = state.listScroll || 0;
}

// The panel reads through a session locked to the workspace root (see
// api/workspace.py): a 403 is that boundary, not a failure — say so, instead of
// echoing the server's bare "path not readable". Everything the agent may read
// after asking (a skills dir under ~, say) lands here.
function viewerErrorText(err) {
  if (err?.status === 403) return t('files.outsideWorkspace');
  return err?.message || t('files.readFailed');
}

/**
 * A click on an image in the preview (the file itself, or one inside a
 * Markdown document) opens it large in the lightbox the chat's attachments
 * use. A linked image keeps its link. True when it handled the click.
 * @param {EventTarget | null} target
 */
function zoomImage(target) {
  const img = target instanceof Element ? target.closest('img') : null;
  if (!img || img.closest('a[href]')) return false;
  openImageLightbox(img.currentSrc || img.src, {
    alt: img.alt,
    // The file's own image downloads the file; an embedded one, itself.
    downloadHref: img.classList.contains('lv-files-img') ? els.download.href : img.src,
  });
  return true;
}

function viewerNote(text, action) {
  const note = document.createElement('div');
  note.className = 'lv-files-note';
  const span = document.createElement('span');
  span.textContent = text;
  note.appendChild(span);
  if (action) note.appendChild(action);
  return note;
}

// Markdown/CSV reuse the transcript's typography by rendering inside the same
// .lv-turn > .lv-body wrapper the chat uses (see styles.css).
function bodyWrapper() {
  const turn = document.createElement('div');
  turn.className = 'lv-turn';
  const body = document.createElement('div');
  body.className = 'lv-body';
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
  table.className = 'lv-files-csv';
  const data = rows.slice(1, CSV_MAX_ROWS + 1);
  const width = rows[0].length;
  // A column is numeric when every filled cell parses as a number: those sort
  // by value (string collation gets "1.5" vs "1.25" and negatives wrong) and
  // align right. An empty cell sorts last either way (sortRows' nulls).
  const isNum = (/** @type {string} */ c) => c.trim() !== '' && Number.isFinite(Number(c));
  const numeric = Array.from({ length: width }, (_, i) => {
    const filled = data.map((r) => r[i] ?? '').filter((c) => c.trim() !== '');
    return filled.length > 0 && filled.every(isNum);
  });
  const keys = data.map((r, n) => [
    ...Array.from({ length: width }, (_, i) => {
      const c = r[i] ?? '';
      if (c.trim() === '') return null;
      return numeric[i] ? Number(c) : c;
    }),
    n, // the row's own index, to put its <tr> back in sorted order
  ]);
  const trs = data.map((r) => {
    const tr = document.createElement('tr');
    r.forEach((cell, i) => {
      const td = document.createElement('td');
      td.textContent = cell;
      if (numeric[i]) td.className = 'lv-num';
      tr.appendChild(td);
    });
    return tr;
  });
  const tbody = document.createElement('tbody');
  tbody.append(...trs);

  // Header cells sort: ascending → descending → the file's own order, as the
  // chat's data tables do (tables.js, whose sortRows this reuses).
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
  let sortCol = -1;
  /** @type {1 | -1} */
  let sortDir = 1;
  const thead = document.createElement('thead');
  const headTr = document.createElement('tr');
  const ths = rows[0].map((name, i) => {
    const th = document.createElement('th');
    th.scope = 'col';
    if (numeric[i]) th.className = 'lv-num';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'lv-files-csv-sort';
    btn.title = t('files.sortBy', { col: name });
    const label = document.createElement('span');
    label.textContent = name;
    const arrow = document.createElement('span');
    arrow.className = 'lv-sort-arrow';
    arrow.setAttribute('aria-hidden', 'true');
    btn.append(label, arrow);
    btn.addEventListener('click', () => {
      if (sortCol !== i) [sortCol, sortDir] = [i, 1];
      else if (sortDir === 1) sortDir = -1;
      else sortCol = -1;
      const order = sortCol < 0 ? keys : sortRows(keys, sortCol, sortDir, collator);
      tbody.replaceChildren(...order.map((k) => trs[/** @type {number} */ (k[width])]));
      ths.forEach((h, j) => {
        const dir = j === sortCol ? sortDir : 0;
        if (dir) h.setAttribute('aria-sort', dir === 1 ? 'ascending' : 'descending');
        else h.removeAttribute('aria-sort');
        /** @type {HTMLElement} */ (h.querySelector('.lv-sort-arrow')).textContent =
          dir === 1 ? '↑' : dir === -1 ? '↓' : '';
      });
    });
    th.append(btn);
    headTr.appendChild(th);
    return th;
  });
  thead.appendChild(headTr);
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

// ---- Diff ---------------------------------------------------------------------
// A tracked file git sees changed has a second view: its diff against HEAD,
// unified, with old/new line numbers. A new file has none — all green says
// nothing the file itself doesn't — and a deleted one has only that.

/** The change-list row of `path` when git has a diff of it to show. */
function diffOf(path) {
  const c = state.git ? state.changes.get(path) : null;
  return c && ['modified', 'renamed', 'deleted'].includes(c.status) ? c : null;
}

/** The "Diff / File" switch above the viewer, for the file on screen. */
function syncViewerViews() {
  const v = state.viewing;
  const c = v ? diffOf(v.path) : null;
  els.viewerViews.classList.toggle('lv-hidden', !c);
  if (!v) return;
  if (!c && v.view === 'diff') {
    void openFile(v.path, { silent: true, view: 'file' }); // committed meanwhile
    return;
  }
  const deleted = c?.status === 'deleted';
  for (const [b, view] of [
    [els.viewDiff, 'diff'],
    [els.viewFile, 'file'],
  ]) {
    b.classList.toggle('lv-active', v.view === view);
    b.setAttribute('aria-selected', String(v.view === view));
  }
  els.viewFile.disabled = deleted;
  els.attach.classList.toggle('lv-hidden', deleted);
}

async function loadDiff(v) {
  let diff;
  try {
    // A rename diffs against its old path, or git sees only a new file.
    const old = state.changes.get(v.path)?.old_path || undefined;
    diff = await api.workspaceDiff({ agent: store.agent, path: v.path, old });
  } catch (err) {
    if (state.viewing === v) {
      els.viewerBody.replaceChildren(viewerNote(err?.message || t('files.diffFailed')));
    }
    return;
  }
  if (state.viewing !== v) return; // another file (or view) took the viewer
  v.diff = diff;
  renderViewerContent();
}

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;
// The header lines worth showing: the rest (index, ---/+++) repeat the path.
const DIFF_META = /^(rename from|rename to|old mode|new mode) /;

function renderDiff(d) {
  if (d.binary) return viewerNote(t('files.diffBinary'));
  if (!d.diff.trim()) return viewerNote(t('files.diffEmpty'));
  const box = document.createElement('div');
  if (d.truncated) box.appendChild(viewerNote(t('files.diffCut')));
  const lines = document.createElement('div');
  lines.className = 'lv-files-diff';
  const line = (cls, oldN, newN, text) => {
    const row = document.createElement('div');
    row.className = cls ? `lv-diff-line ${cls}` : 'lv-diff-line';
    for (const n of [oldN, newN]) {
      const num = document.createElement('span');
      num.className = 'lv-diff-n';
      num.textContent = n === null ? '' : String(n);
      row.appendChild(num);
    }
    const code = document.createElement('span');
    code.className = 'lv-diff-code';
    code.textContent = text;
    row.appendChild(code);
    lines.appendChild(row);
  };
  let oldN = 0;
  let newN = 0;
  let inHunk = false; // before the first @@ are git's headers (diff --git, index, ---, +++)
  for (const text of d.diff.split('\n')) {
    const m = HUNK.exec(text);
    // A rename git didn't pair up diffs as two files: the second's headers
    // follow the first's last hunk.
    if (text.startsWith('diff --git ')) inHunk = false;
    else if (m) {
      [oldN, newN, inHunk] = [Number(m[1]), Number(m[2]), true];
      line('lv-hunk', null, null, text);
    } else if (!inHunk && DIFF_META.test(text)) {
      // What a pure rename or a mode change consists of: it has no hunk.
      line('lv-hunk', null, null, text);
    } else if (!inHunk || !text || text.startsWith('\\')) {
      // a header, the closing newline, "\ No newline at end of file"
    } else if (text[0] === '+') {
      line('lv-add', null, newN++, text);
    } else if (text[0] === '-') {
      line('lv-del', oldN++, null, text);
    } else {
      line('', oldN++, newN++, text);
    }
  }
  box.appendChild(lines);
  return box;
}

// Returns false when the file couldn't be read (so link-following can retry
// with a different base); true otherwise. `view` picks diff or file for a
// file that has both; a silent reopen (an agent write) keeps the one on screen.
async function openFile(path, { silent = false, view = undefined } = {}) {
  const name = basename(path);
  rememberViewing(path);
  if (!els.panel.classList.contains('lv-viewing')) state.listScroll = els.list.scrollTop;
  els.viewer.classList.remove('lv-hidden');
  els.panel.classList.add('lv-viewing');
  // The header is the file's crumbs: its folders lead back into Browse. An
  // absolute path (outside the workspace) has no folders there to open, so
  // it stays text, truncated from the left (rtl) to keep the filename; the
  // bidi isolate keeps a leading "~/." from being reordered to the far end.
  if (path.startsWith('/')) {
    const abs = document.createElement('span');
    abs.className = 'lv-files-viewer-abs';
    const label = document.createElement('bdi');
    label.textContent = path;
    abs.appendChild(label);
    els.viewerName.replaceChildren(abs);
  } else {
    fillCrumbs(els.viewerName, path, browseTo, { fold: true });
  }
  els.viewerName.title = path;
  els.download.href = api.workspaceRawUrl({ agent: store.agent, path, download: true });
  // Off until renderViewerContent re-shows them for text: an image, PDF or
  // binary returns before it, and must not keep the last file's buttons.
  els.mdToggle.classList.add('lv-hidden');
  els.wrapToggle?.classList.add('lv-hidden');
  els.copyContent.classList.add('lv-hidden');

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
  const same = silent && state.viewing?.path === path ? state.viewing : null;
  const change = diffOf(path);
  const shown =
    change?.status === 'deleted' ? 'diff' : change ? (view ?? same?.view ?? 'file') : 'file';
  // The last diff stays up while a fresh one loads: an agent's edits don't strobe it.
  const diff = shown === 'diff' ? same?.diff : undefined;
  state.viewing = { path, kind, name, raw: same?.raw ?? false, view: shown, diff };
  if (!silent) renderList();
  syncViewerViews();
  if (shown === 'diff') {
    const v = state.viewing;
    if (diff) renderViewerContent();
    else els.viewerBody.replaceChildren();
    await loadDiff(v);
    return true;
  }

  if (kind === 'image') {
    const img = document.createElement('img');
    img.className = 'lv-files-img';
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
    embed.className = 'lv-files-frame';
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
      const res = await api.workspaceRaw({ agent: store.agent, path, download: true });
      // Carries the status, so a refusal reads as the boundary it is.
      if (!res.ok) throw await apiError(res);
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
    dl.className = 'lv-btn lv-btn-ghost lv-btn-sm';
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
 * images all clone faithfully; the interactive controls inside — "load
 * more", a code block's Copy, a CSV header's sort, an image's zoom — are
 * delegated back to the panel above. A diagram still rendering when the panel
 * was cloned renders into the copy too.
 */
function syncModal() {
  const copy = /** @type {HTMLElement} */ (els.viewerBody.cloneNode(true));
  copy.removeAttribute('id');
  els.modalBody.replaceChildren(copy);
  renderMermaid(copy);
}

function syncWrapButton() {
  els.wrapToggle.innerHTML = icon('wrap-text', { size: 15 });
  els.wrapToggle.title = state.wrap ? t('files.nowrap') : t('files.wrap');
  els.wrapToggle.setAttribute('aria-label', els.wrapToggle.title);
  els.wrapToggle.classList.toggle('lv-active', state.wrap);
}

function renderViewerContent() {
  const v = state.viewing;
  if (!v) return;
  els.viewerBody.replaceChildren();
  els.viewerBody.classList.toggle('lv-nowrap', !state.wrap);
  if (v.view === 'diff') {
    if (!v.diff) return; // still loading
    els.mdToggle.classList.add('lv-hidden');
    els.wrapToggle.classList.toggle('lv-hidden', !v.diff.diff);
    els.copyContent.classList.toggle('lv-hidden', !v.diff.diff);
    els.viewerBody.appendChild(renderDiff(v.diff));
    return;
  }
  // Wrap toggling only means something for plain text / raw views.
  const textual =
    v.kind === 'text' ||
    v.kind === 'csv' ||
    ((v.kind === 'md' || v.kind === 'html') && v.raw);
  els.wrapToggle.classList.toggle('lv-hidden', !textual);
  // Copy only means something when there is text content to copy.
  els.copyContent.classList.toggle('lv-hidden', typeof v.content !== 'string');

  if (v.truncated) {
    const more = document.createElement('button');
    more.type = 'button';
    // The class marks it for the reader modal, whose cloned copy delegates
    // back to this same loadMore (a clone carries no listeners of its own).
    more.className = 'lv-btn lv-btn-ghost lv-btn-sm lv-files-load-more';
    more.textContent = t('files.loadMore');
    more.addEventListener('click', loadMore);
    els.viewerBody.appendChild(
      viewerNote(t('files.showingLines', { end: v.end, total: v.totalLines }), more),
    );
  }

  if (v.kind === 'md' || v.kind === 'html') {
    els.mdToggle.classList.remove('lv-hidden');
    els.mdToggle.innerHTML = icon(v.raw ? 'file-text' : 'code', { size: 15 });
    els.mdToggle.title = v.raw ? t('files.rendered') : t('files.raw');
    els.mdToggle.setAttribute('aria-label', els.mdToggle.title);
  }
  if (v.kind === 'md' && !v.raw) {
    const { turn, body } = bodyWrapper();
    // Images resolve like the links below: against the document's own
    // directory first, then the workspace root.
    renderMarkdownInto(body, v.content, { agent: store.agent, base: dirname(v.path) });
    highlightIn(body);
    addCodeBlockControls(body); // language + Copy, as in the chat
    els.viewerBody.appendChild(turn);
    renderMermaid(body); // attached first: a cached diagram swaps only into a live <pre>
    return;
  }
  if (v.kind === 'html' && !v.raw) {
    // Agent-authored HTML: an opaque origin that can't touch the parent DOM,
    // the token cookie or storage, and can't navigate itself away.
    els.viewerBody.appendChild(
      sandboxFrame(v.content, { title: v.name, className: 'lv-files-frame lv-files-frame--html' }),
    );
    return;
  }
  if (v.kind === 'csv' && !v.truncated) {
    const table = renderCsv(v.content, ext(v.path) === 'tsv' ? '\t' : ',');
    if (table) {
      els.wrapToggle.classList.add('lv-hidden'); // a table keeps its cells on one line
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
      // The user brought it into this chat: it lists with the chat's own
      // (until the chat changes — it is on no message to replay from).
      state.touched.add((await api.uploadFile(file, { agent: store.agent })).path);
      ok += 1;
    } catch (err) {
      toast(uploadFailedText(file.name, err), { type: 'error' });
    }
  }
  if (ok) {
    toast(t('files.uploaded', { n: ok }));
    if (state.mode !== 'changes') setMode('changes'); // that's where they surface
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
    els.panel.classList.add('lv-drag-over');
  });
  els.panel.addEventListener('dragleave', (e) => {
    if (!els.panel.contains(/** @type {Node} */ (e.relatedTarget))) {
      els.panel.classList.remove('lv-drag-over');
    }
  });
  els.panel.addEventListener('drop', (e) => {
    els.panel.classList.remove('lv-drag-over');
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
/** Wire up the Files panel: open/close, Changes/Browse tabs, the viewer, and workspace-change listeners. */
export function initFiles() {
  els.panel = document.getElementById('files-panel');
  els.btn = document.getElementById('files-btn');
  if (!els.panel || !els.btn) return;
  els.tabChanges = document.getElementById('files-tab-changes');
  els.viewerViews = document.getElementById('files-viewer-views');
  els.viewDiff = document.getElementById('files-view-diff');
  els.viewFile = document.getElementById('files-view-file');
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
  els.viewerClose.title = t('files.closeFile');
  els.viewerClose.setAttribute('aria-label', els.viewerClose.title);
  if (els.upload) els.upload.innerHTML = icon('upload', { size: 15 });
  // A narrow panel folds the less-used actions — attach, line wrap, download —
  // into "⋯" (CSS decides when): each item clicks the real button, so its
  // handler and its hidden state (no wrap for an image) are the button's own.
  els.more = document.getElementById('files-more');
  if (els.more) {
    els.more.innerHTML = icon('ellipsis', { size: 15 });
    els.more.addEventListener('click', () => {
      const shown = (/** @type {HTMLElement} */ b) => !b.classList.contains('lv-hidden');
      /** @type {import('./ui.js').MenuItem[]} */
      const items = [];
      if (shown(els.attach)) {
        items.push({ label: t('files.attach'), icon: icon('paperclip', { size: 15 }), onSelect: () => els.attach.click() });
      }
      if (shown(els.wrapToggle)) {
        items.push({
          label: t('files.wrap'),
          icon: icon('wrap-text', { size: 15 }),
          checked: state.wrap,
          onSelect: () => els.wrapToggle.click(),
        });
      }
      items.push({ label: t('files.download'), icon: icon('download', { size: 15 }), onSelect: () => els.download.click() });
      openMenu(els.more, items);
    });
  }

  if (els.resizer) initResizer();
  initLayout();
  initUpload();

  els.btn.addEventListener('click', () => setOpen(!state.open));
  els.close.addEventListener('click', () => setOpen(false));
  els.refresh.addEventListener('click', () => {
    refresh();
    refreshProcs();
  });
  els.tabChanges.addEventListener('click', () => setMode('changes'));
  els.tabBrowse.addEventListener('click', () => setMode('browse'));
  for (const [b, view] of [
    [els.viewDiff, 'diff'],
    [els.viewFile, 'file'],
  ]) {
    b.addEventListener('click', () => {
      const v = state.viewing;
      if (v && v.view !== view) void openFile(v.path, { silent: true, view });
    });
  }
  els.viewerClose.addEventListener('click', () => closeViewer());
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
    const v = state.viewing;
    const content = v?.view === 'diff' ? v.diff?.diff : v?.content;
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
  // The reader holds a clone, and clones carry no listeners — delegate its
  // live controls back to the panel, then re-mirror. "Load more" extends the
  // page; a workspace link keeps the reader reading, same as the viewer below
  // (a modified click still follows the href into a tab of its own); a code
  // block's Copy copies from the clone itself.
  els.modalBody.addEventListener('click', async (e) => {
    const target = e.target instanceof Element ? e.target : null;
    if (zoomImage(target)) return;
    // A CSV header in the clone sorts the panel's table (the one with the
    // listeners), then the reader re-mirrors it.
    const sortBtn = target?.closest('.lv-files-csv-sort');
    if (sortBtn) {
      const col = [...(sortBtn.closest('tr')?.children || [])].indexOf(sortBtn.parentElement);
      /** @type {HTMLElement | undefined} */
      ([...els.viewerBody.querySelectorAll('.lv-files-csv-sort')][col])?.click();
      syncModal();
      return;
    }
    const copyBtn = target?.closest('.lv-btn-copy-code');
    if (copyBtn) {
      void copyCodeBlock(copyBtn);
      return;
    }
    if (target?.closest('.lv-files-load-more')) {
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
    if (zoomImage(e.target)) return;
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
    // An open dialog (the reader, a lightbox) or menu ("⋯") owns Escape — it
    // closes itself — otherwise one keypress would close the viewer under it too.
    if (document.querySelector('dialog[open]') || menuOpen()) return;
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
    clearUnseen();
    closeViewer({ forget: false });
    clearList();
    updateVisibility();
    if (state.open) {
      refresh();
      refreshProcs();
    }
  });
  // The panel shows the directory the chat on screen works in (a draft's,
  // for a new chat): a different one is a different tree — start over.
  store.on('workspace-scope-changed', () => {
    state.browsePath = '';
    state.filter = '';
    if (els.filter) els.filter.value = '';
    closeViewer({ forget: false });
    clearList();
    if (state.open) refresh();
  });
  // "touched" (and the edit revisions) are scoped to the chat on screen —
  // and so are background processes. The list drops at once what the chat
  // just left wrote; the replay of the new one's history asks for its own.
  const chatChanged = () => {
    state.touched.clear();
    state.revs.clear();
    clearUnseen();
    state.entries = state.entries.filter((e) => !e.touched);
    queueRender();
    queueRefresh();
    state.procs = [];
    renderProcs();
    refreshProcs();
  };
  store.on('session-switched', chatChanged);
  store.on('reset-chat-view', chatChanged);
  store.on('workspace-file-touched', async ({ path, attached = false }) => {
    const key = touchKey(path);
    state.touched.delete(key); // re-added last: the set stays in touch order
    state.touched.add(key);
    queueRefresh();
    if (attached) return; // listed, but nothing about the file changed
    state.revs.set(key, (state.revs.get(key) || 0) + 1);
    maybeReloadViewing(key);
    // Badge only live-run writes of deliverables: replayed history re-emits
    // old touches, and a scratch write never reaches the change list the
    // badge invites the user to open. Decided once the scratch dir is known.
    if (state.open || !store.streaming) return;
    const agent = store.agent;
    const scratch = await scratchDirFor(agent);
    if (store.agent !== agent || state.open || isScratch(key, scratch)) return;
    state.unseen.add(key);
    renderBadge();
  });
  store.on('workspace-maybe-stale', () => {
    state.stale = true;
    if (state.open) els.refresh.classList.add('lv-stale');
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
