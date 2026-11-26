// The right panel: the Files panel — a read-only window into the agent's
// workspace — and the shell around the Background tab, whose list is
// background.js's.
//
// lovia is a personal assistant, so the panel leads with "Changes": the files
// this chat wrote or brought in, newest first, then — in a git work tree — the
// other uncommitted changes, folded. "Browse" (a folder tree that opens in
// place) is the secondary view. The viewer renders documents as documents:
// markdown rich by default, images inline, CSV as a table; code gets
// highlighting; a changed tracked file also offers its diff against HEAD.
// "Background" needs no workspace, so it is what the panel shows for an agent
// without one. The viewer takes guests besides files (`openGuest`): a
// command's log (process-view.js).
//
// Wiring: chat.js emits `workspace-file-touched` for successful
// write_file/edit_file calls and for a sent message's attachments (live and
// replayed history), and `workspace-maybe-stale` after a shell run; this
// module owns everything else.
import { t } from './i18n.js';
import { store } from './store.js';
import { api, apiError } from './api.js';
import {
  CHAT_MIN_W,
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
import { isRemote, scopeRoot } from './workspaces.js';
import { desktopItems } from './desktop.js';
import { diffLanguage, diffSource } from './diff.js';
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
  // The tab on: 'browse' is the one named Files.
  mode: /** @type {'changes' | 'browse' | 'background'} */ ('changes'),
  fileMode: /** @type {'changes' | 'browse'} */ ('changes'), // of the two file tabs, the one last on
  entries: [], // the change list (Browse keeps its listings in `tree`)
  // The last change list, by path — what the viewer asks whether a file has
  // a diff to show (also while browsing). `git` says whether there is one at all.
  changes: new Map(),
  git: false,
  truncated: false, // git listed more than the list shows
  othersOpen: false, // "other uncommitted changes" unfolded
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
  listBeside: localStorage.getItem('lovia-files-list') !== '0', // beside an open file, given room
  viewing: null, // { path, kind, raw, name, end, totalLines, truncated, view, diff }, or { name, guest }
  listScroll: 0, // the list's scroll when a file covered it
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

const isPhone = () => window.matchMedia('(max-width: 720px)').matches;
const panelWidth = () => els.panel.getBoundingClientRect().width;

/**
 * @param {number | null} px
 * @param {boolean} [beside] A drag: stop short of the sidebar as it stands,
 *   so the chat column keeps its room without anything collapsing.
 */
function applyWidth(px, beside = false) {
  if (px == null) return void document.documentElement.style.removeProperty('--lv-files-w');
  const sidebar = beside && !isPhone() ? document.getElementById('sidebar') : null;
  const room = window.innerWidth - CHAT_MIN_W - (sidebar?.getBoundingClientRect().width ?? 0);
  const w = Math.min(Math.max(Math.round(px), MIN_W), Math.max(MIN_W, room));
  document.documentElement.style.setProperty('--lv-files-w', `${w}px`);
}

// Three columns need room. When the panel opens (or the window shrinks) on a
// viewport too tight for sidebar + panel + a comfortable chat column, it
// claims the sidebar's space; the claim is released on close or once there
// is room, and beaten by an explicit expand (the two layers live in ui.js).
// A divider drag never claims — the user is arranging the columns, and each
// drag stops short of the chat column instead — but may give the room back.
// `release` only lets go.
function claimSpace({ release = false } = {}) {
  // Closed, the claim goes at any width — a close on a phone included, or
  // an overruled claim would outlive the panel back on the desktop.
  if (!state.open) {
    setSidebarAutoCollapsed(false);
    return;
  }
  if (isPhone()) return; // the phone drawer overlays, it doesn't push
  const sidebarW =
    parseInt(
      getComputedStyle(document.documentElement).getPropertyValue('--lv-sidebar-w'),
      10,
    ) || 272;
  const tight = window.innerWidth < sidebarW + panelWidth() + 760;
  if (!(release && tight)) setSidebarAutoCollapsed(tight);
}

function initResizer() {
  const saved = Number(localStorage.getItem(WIDTH_KEY));
  if (saved) applyWidth(saved);

  widthDivider(els.resizer, {
    width: panelWidth,
    apply: (px) => applyWidth(px, true),
    grow: -1, // the panel sits right: dragging left widens it
    busyClass: 'lv-files-resizing',
    done: (reset) => {
      if (reset) localStorage.removeItem(WIDTH_KEY);
      else localStorage.setItem(WIDTH_KEY, String(Math.round(panelWidth())));
      // A reset jumps to the default width unclamped: judged as an opening.
      claimSpace({ release: !reset });
    },
  });

  // A window resize can re-tighten (or free) the space the open panel needs.
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(claimSpace, 120);
  });
}

// ---- List / preview layout --------------------------------------------------
// An open file takes the panel (`lv-viewing`): on a narrow panel the list
// steps aside and the tabs, the header's folder or Esc lead back; once the panel is dragged
// wide enough (`lv-files-roomy`), the list stays beside the preview instead
// (`lv-files-wide`) — unless the header's toggle hid it: then the panel works
// as a narrow one. Width is the panel's own, so a ResizeObserver decides — not
// the viewport.
const WIDE_W = 720;
const isWide = () => els.panel.classList.contains('lv-files-wide');

function syncLayout() {
  const roomy = els.panel.clientWidth >= WIDE_W;
  fitTabs();
  els.panel.classList.toggle('lv-files-roomy', roomy);
  els.panel.classList.toggle('lv-files-wide', roomy && state.listBeside);
}

/** The Changes count shows while the tabs have room for it: names first. */
function fitTabs() {
  els.panel.classList.remove('lv-files-snug');
  const tabs = els.panel.querySelectorAll('.lv-files-head .lv-files-tab');
  const cut = [...tabs].some((tab) => tab.scrollWidth > tab.clientWidth + 1);
  els.panel.classList.toggle('lv-files-snug', cut);
}

function initLayout() {
  new ResizeObserver(syncLayout).observe(els.panel);
}

function syncListToggle() {
  els.listToggle.title = t(state.listBeside ? 'files.hideList' : 'files.showList');
  els.listToggle.setAttribute('aria-label', els.listToggle.title);
}

function toggleList() {
  // A hidden list loses its scroll: keep it for when the list is back.
  if (isWide()) state.listScroll = listEl().scrollTop;
  state.listBeside = !state.listBeside;
  localStorage.setItem('lovia-files-list', state.listBeside ? '1' : '0');
  syncListToggle();
  syncLayout();
  if (isWide()) listEl().scrollTop = state.listScroll;
}

/** The list of the tab that is on. */
const listEl = () => (state.mode === 'background' ? els.background : els.list);

// ---- Panel open/close -----------------------------------------------------
/** The file tabs are what the user sees. */
const filesShown = () => state.open && state.mode !== 'background';

// `persist: false` is for forced closes (agent without a workspace) — they
// must not overwrite the user's remembered open/closed preference. Nor does
// what only the Background tab can be opened for.
function setOpen(open, { persist = true } = {}) {
  if (open && !state.available) selectTab('background');
  state.open = open;
  els.panel.classList.toggle('lv-open', state.open);
  els.btn?.setAttribute('aria-expanded', String(filesShown()));
  if (persist && state.available) {
    localStorage.setItem('lovia-files-open', state.open ? '1' : '0');
  }
  claimSpace();
  if (filesShown()) clearUnseen(); // the badge's job is done — the user is looking
  if (state.open) refresh();
  store.emit('panel-changed');
}

/** @returns {{ open: boolean, mode: string, files: boolean }} `files`: whether the file tabs are there. */
export function panelState() {
  return { open: state.open, mode: state.mode, files: state.available };
}

/**
 * Show the panel at a tab, or close it (null).
 * @param {'background' | null} mode
 * @param {{ restore?: boolean }} [opts] `restore: false` for a caller about
 *   to open something there itself: what was open there stays closed.
 */
export function setPanel(mode, { restore = true } = {}) {
  if (mode === null) return setOpen(false);
  if (state.mode !== mode) void setMode(mode, { restore });
  if (!state.open) setOpen(true);
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
function scratchDirFor(agent) {
  if (_info?.agent !== agent) {
    const promise = api.workspaceInfo({ agent }).then(
      (info) => info.scratch_dir || null,
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
  els.tabChanges.classList.toggle('lv-hidden', !state.available);
  els.tabBrowse.classList.toggle('lv-hidden', !state.available);
  if (state.available) scratchDirFor(store.agent); // warm it for the first write
  const phone = window.matchMedia('(max-width: 720px)').matches;
  if (!state.available) {
    if (filesShown()) setOpen(false, { persist: false });
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
  if (!state.open || !state.available || _refreshTimer) return;
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
    if (filesShown()) renderList();
  });
}

async function refresh() {
  clearTimeout(_refreshTimer);
  _refreshTimer = null;
  _refreshAgain = false;
  if (!state.open || !state.available) return;
  // Off the Changes tab its count still shows: the change list is asked for
  // beside the tab's own listing (Files), or alone (Background).
  if (state.mode !== 'changes') void refreshChangeMap();
  if (!filesShown()) return;
  const seq = ++_refreshSeq;
  _refreshing = true;
  state.stale = false;
  els.refresh.classList.remove('lv-stale');
  els.refresh.classList.add('lv-loading');
  // An empty list says it is on its way only once the wait is felt (a
  // folder on another machine): a local one answers before the eye asks.
  clearTimeout(_slowTimer);
  _slowTimer = setTimeout(() => {
    const empty = state.mode === 'changes' ? !state.entries.length : !tree.listings.has('');
    if (empty) els.list.replaceChildren(emptyNode(t('files.loading')));
  }, 300);
  let changes = null;
  let listings = null;
  let failure = null;
  try {
    if (state.mode === 'changes') changes = await fetchChanges();
    else listings = await fetchTree();
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
    retry.className = 'lv-btn lv-btn-ghost lv-btn-sm lv-files-empty-action';
    retry.innerHTML = icon('refresh-cw', { size: 14 });
    retry.append(t('files.retry'));
    retry.addEventListener('click', refresh);
    failed.append(retry);
    els.list.replaceChildren(failed);
    return;
  }
  if (changes) {
    state.entries = changes.files;
    state.truncated = changes.truncated;
    applyChangeMap(changes);
  } else {
    applyTree(listings);
  }
  renderList();
  void restoreViewing();
}

function fetchChanges() {
  // The server takes 500 at most: the most recently touched, in a chat that long.
  const paths = [...state.touched].slice(-MAX_TOUCHED);
  return api.workspaceChanges({ agent: store.agent, paths });
}

/** Take a change list as what the viewer knows of diffs. */
function applyChangeMap(changes) {
  state.git = changes.git;
  state.changes = new Map(changes.files.map((f) => [f.path, f]));
  syncViewerViews(); // a file on screen may just have gained (or lost) a diff
  renderChangeStat();
}

/** The Changes tab's "+12 −3": the lines this chat's own files changed. */
function renderChangeStat() {
  let added = 0;
  let removed = 0;
  for (const f of state.changes.values()) {
    if (!f.touched) continue;
    added += f.added || 0;
    removed += f.removed || 0;
  }
  const stat = els.changesStat;
  if (!stat) return;
  stat.replaceChildren();
  if (!added && !removed) return;
  const part = (/** @type {string} */ cls, /** @type {string} */ text) => {
    const span = document.createElement('span');
    span.className = cls;
    span.textContent = text;
    stat.append(span);
  };
  if (added) part('lv-add', `+${added}`);
  if (removed) part('lv-del', `\u2212${removed}`);
  fitTabs();
}

// Off the Changes tab the change map is asked for on its own: for the tab's
// count, and so a changed file opens with its Diff tab. Opening (or reloading)
// a file asks for it too; the file shows at once.
let _mapSeq = 0;
async function refreshChangeMap() {
  const seq = ++_mapSeq;
  const scope = viewingScope();
  let changes;
  try {
    changes = await fetchChanges();
  } catch {
    return; // the map stays as it was: at worst a tab missing, as before
  }
  // A newer ask, another agent or folder, or the Changes tab took over.
  if (seq !== _mapSeq || scope !== viewingScope() || state.mode === 'changes') return;
  applyChangeMap(changes);
}

/** The list has nothing of the place just left to show while the new one loads. */
function clearList() {
  state.entries = [];
  state.changes = new Map();
  state.git = false;
  state.failed = false;
  els.list.replaceChildren();
  renderChangeStat();
}

function emptyNode(text) {
  const div = document.createElement('div');
  div.className = 'lv-files-empty';
  div.textContent = text;
  return div;
}

/** Put a tab on, without loading what it lists. */
function selectTab(mode) {
  state.mode = mode;
  if (mode !== 'background') state.fileMode = mode;
  _mapSeq++; // a Browse map ask still out predates whatever this tab loads
  for (const [tab, on] of [
    [els.tabChanges, mode === 'changes'],
    [els.tabBrowse, mode === 'browse'],
    [els.tabBackground, mode === 'background'],
  ]) {
    tab.classList.toggle('lv-active', on);
    tab.setAttribute('aria-selected', String(on));
  }
  els.panel.classList.toggle('lv-background', mode === 'background');
  els.btn?.setAttribute('aria-expanded', String(filesShown()));
  if (els.filter) els.filter.placeholder = t(mode === 'browse' ? 'files.filterTree' : 'files.filter');
  if (mode === 'browse') els.list.setAttribute('role', 'tree');
  else els.list.removeAttribute('role');
  if (mode === 'background') {
    // Whatever file listing is on its way is no longer asked for.
    _refreshSeq++;
    _refreshing = false;
    clearTimeout(_slowTimer);
    els.refresh.classList.remove('lv-loading', 'lv-stale');
    els.collapse?.classList.add('lv-hidden');
  }
}

/**
 * @param {'changes' | 'browse' | 'background'} mode
 * @param {{ restore?: boolean, carry?: boolean }} [opts] `restore: false` for
 *   a caller about to open something there itself; `carry` takes the open
 *   file along instead.
 */
async function setMode(mode, { restore = true, carry = false } = {}) {
  if (mode !== state.mode) {
    // Each tab keeps what it had open — a diff, a file, a log or a task — and
    // gets it back on its return.
    if (state.viewing && !carry) parkViewer(state.mode);
    selectTab(mode);
    // Taken now, whatever happens next: a caller opening something of its
    // own there means the last one is not coming back.
    const slot = parked[mode];
    parked[mode] = null;
    if (restore && !carry) void unpark(slot);
  } else {
    // A tab is also the way back to its list from a file covering the panel.
    if (state.viewing && !isWide()) closeViewer();
    selectTab(mode);
  }
  if (filesShown()) clearUnseen();
  store.emit('panel-changed');
  await refresh();
}

/** @typedef {{ path: string, view: string, raw: boolean }} ParkedFile */
/** What each tab had open while another one was on. */
const parked = {
  changes: /** @type {ParkedFile | null} */ (null),
  browse: /** @type {ParkedFile | null} */ (null),
  background: /** @type {ViewerGuest | null} */ (null),
};

function parkViewer(mode) {
  const v = state.viewing;
  if (mode === 'background') parked.background = v?.guest ?? null;
  else parked[mode] = v?.path ? { path: v.path, view: v.view, raw: v.raw } : null;
  closeViewer({ forget: false });
}

/** @param {ParkedFile | ViewerGuest | null} slot */
async function unpark(slot) {
  if (!slot) return;
  if (!('path' in slot)) return openGuest(slot);
  await openFile(slot.path, { silent: true, view: slot.view });
  const v = state.viewing;
  if (v?.path !== slot.path) return;
  // As it was shown: its diff or its source, not just the file — and the
  // diff may have come or gone meanwhile.
  if (state.mode !== 'changes') void refreshChangeMap();
  if (slot.raw && !v.raw && typeof v.content === 'string') {
    v.raw = true;
    renderViewerContent();
  }
}

// ---- Browse: the folder tree -------------------------------------------------
// A folder opens in place, its listing fetched on first open; a refresh
// re-lists every open folder in view. A folder whose only child is a folder
// shows the chain as one row ("src/app/core"), and an open folder's row
// sticks to the top while its children scroll under it, so a deep position
// keeps its context. What is open persists per tab and scope, as the open
// file does.
const TREE_KEY = 'lovia-files-tree';
const TREE_PAGE = 200; // rows a folder shows before "show more"
const STICKY_DEPTH = 4; // open folders deeper than this scroll away instead of stacking
const tree = {
  scope: /** @type {string | null} */ (null),
  /** @type {Map<string, any[]>} folder ('' = the root) → its entries */
  listings: new Map(),
  /** Open folders — one inside a closed folder stays open for when it shows again. */
  open: new Set(),
  /** @type {Map<string, number>} folder → rows shown, in TREE_PAGE steps */
  shown: new Map(),
  loading: new Set(), // folders whose first listing is on its way
  cut: new Set(), // folders the workspace's listing cap cut short
};

/** Keep a folder's listing (`{ files, truncated }`) under the path it was opened by. */
function setListing(dir, listing) {
  tree.listings.set(dir, underDir(dir, listing.files));
  if (listing.truncated) tree.cut.add(dir);
  else tree.cut.delete(dir);
}

/** 'a/b/c' → ['a', 'a/b']: the folders above a path. */
const ancestorsOf = (path) => {
  const parts = path.split('/');
  return parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join('/'));
};

/** The tree belongs to the agent and folder on screen: another one starts from what it left. */
function syncTreeScope() {
  const scope = viewingScope();
  if (tree.scope === scope) return;
  tree.scope = scope;
  tree.listings.clear();
  tree.shown.clear();
  tree.loading.clear();
  tree.cut.clear();
  tree.open = new Set();
  try {
    const saved = JSON.parse(sessionStorage.getItem(TREE_KEY) || 'null');
    if (saved?.scope === scope && Array.isArray(saved.open)) {
      tree.open = new Set(saved.open.filter((p) => typeof p === 'string'));
    }
  } catch {
    /* unreadable: start closed */
  }
}

function saveTree() {
  try {
    sessionStorage.setItem(TREE_KEY, JSON.stringify({ scope: tree.scope, open: [...tree.open] }));
  } catch {
    /* storage off: this tab just won't remember */
  }
}

/**
 * A folder's subfolders, named under the path it was opened by. Through a
 * symlinked folder the server names entries where they really are (`bl ->
 * blog` lists `blog/x`, `loop -> .` lists `big`, `loop`, …): kept that way, a
 * link back up would hold itself and the tree recurse forever. Renamed, each
 * level of a loop is one more folder to open, as in a file manager. Folders
 * are only the tree's own keys; a file keeps its real path — its identity for
 * the change map, write notices, and everything it opens with.
 */
function underDir(dir, entries) {
  if (!dir) return entries;
  return entries.map((e) =>
    !e.is_dir || e.path.startsWith(`${dir}/`) ? e : { ...e, path: `${dir}/${basename(e.path)}` },
  );
}

/** Open folders whose every ancestor is open: the ones a refresh re-lists. */
const shownDirs = () =>
  [...tree.open].filter((d) => ancestorsOf(d).every((a) => tree.open.has(a)));

/** The root and every open folder in view, listed at once. Only the root's failure fails. */
async function fetchTree() {
  syncTreeScope();
  const dirs = ['', ...shownDirs()];
  const agent = store.agent;
  const got = await Promise.allSettled(
    dirs.map((d) => api.workspaceFiles({ agent, path: d || '.' })),
  );
  if (got[0].status === 'rejected') throw got[0].reason;
  return dirs.map((d, i) => /** @type {const} */ ([d, got[i]]));
}

/** Take a refresh's listings; a folder that no longer lists (gone) closes. */
function applyTree(listings) {
  // A closed folder's listing is stale by now: it re-lists on open. One that
  // opened while this refresh was out has a listing newer than it: it stays.
  const fetched = new Set(listings.map(([dir]) => dir));
  const shown = new Set(shownDirs());
  for (const dir of tree.listings.keys()) {
    if (!fetched.has(dir) && !shown.has(dir)) {
      tree.listings.delete(dir);
      tree.cut.delete(dir);
    }
  }
  for (const [dir, r] of listings) {
    if (r.status === 'fulfilled') setListing(dir, r.value);
    else tree.open.delete(dir);
  }
  saveTree();
}

/** The folder a folder holds when it holds nothing else — the next link of a chain. */
function soleDir(dir) {
  const entries = tree.listings.get(dir);
  const only = entries?.length === 1 ? entries[0] : null;
  return only?.is_dir && !only.symlink_target ? only : null;
}

/** `entry` and the lone folders below it, as far as their listings are known. */
function chainFrom(entry) {
  const chain = [entry];
  for (let next = soleDir(entry.path); next; next = soleDir(next.path)) chain.push(next);
  return chain;
}

/** Open a folder: fetch it if need be, and a lone folder inside opens with it (one row). */
async function openDir(path) {
  const scope = tree.scope;
  tree.open.add(path);
  for (let dir = path, hops = 0; dir && hops < 64; hops++) {
    if (!tree.listings.has(dir)) {
      tree.loading.add(dir);
      // "Loading…" under the row only once the wait is felt.
      const slow = setTimeout(queueRender, 250);
      let listing;
      try {
        listing = await api.workspaceFiles({ agent: store.agent, path: dir });
      } catch (err) {
        if (tree.scope !== scope) return;
        tree.open.delete(dir);
        toast(err?.message || t('files.loadFailed'), { type: 'error' });
        break;
      } finally {
        clearTimeout(slow);
        tree.loading.delete(dir);
      }
      if (tree.scope !== scope) return; // another agent or folder meanwhile
      setListing(dir, listing);
      if (!tree.open.has(dir)) break; // closed again while it loaded
    }
    const only = soleDir(dir);
    if (only) tree.open.add(only.path);
    dir = only?.path;
  }
  saveTree();
  queueRender();
}

/** A folder row's click: a chain opens and closes as the one row it is. */
function toggleDir(chain) {
  if (chain.every((e) => tree.open.has(e.path))) {
    for (const e of chain) tree.open.delete(e.path);
    saveTree();
    renderList();
    return;
  }
  for (const e of chain.slice(0, -1)) tree.open.add(e.path);
  void openDir(chain[chain.length - 1].path);
  renderList(); // the chevron turns now; the children follow their listing
}

const nameHit = (entry, needle) => basename(entry.path).toLowerCase().includes(needle);

/** A note in the tree's own indent: "Loading…", "Empty directory.". */
function treeNote(text, depth) {
  const div = document.createElement('div');
  div.className = 'lv-tree-note';
  div.style.setProperty('--depth', String(depth));
  div.textContent = text;
  return div;
}

/**
 * One folder's rows, and under each open folder its own. Filtering searches
 * only what is listed — every row of an open folder, not the folders closed
 * — and keeps the folders above a hit.
 * @returns {Node[]}
 */
function treeLevel(dir, depth, needle) {
  const entries = tree.listings.get(dir) || [];
  const limit = needle ? entries.length : tree.shown.get(dir) || TREE_PAGE;
  const nodes = [];
  for (const entry of entries.slice(0, limit)) {
    if (!entry.is_dir) {
      // Its place in the tree: the same file can show under a folder and a link to it.
      const head = dir ? `${dir}/${basename(entry.path)}` : entry.path;
      if (!needle || nameHit(entry, needle)) nodes.push(rowFor(entry, { depth, head }));
      continue;
    }
    const chain = chainFrom(entry);
    const last = chain[chain.length - 1];
    const open = chain.every((e) => tree.open.has(e.path));
    const kids = open && tree.listings.has(last.path) ? treeLevel(last.path, depth + 1, needle) : null;
    if (needle && !chain.some((e) => nameHit(e, needle)) && !kids?.length) continue;
    const node = document.createElement('div');
    node.className = 'lv-tree-node';
    node.appendChild(rowFor(entry, { depth, chain, open }));
    const group = document.createElement('div');
    // No role="group": it would need to sit inside its treeitem (a button
    // here); each row's aria-level carries the nesting instead.
    group.className = 'lv-tree-group';
    group.style.setProperty('--depth', String(depth + 1));
    if (kids?.length) group.append(...kids);
    else if (kids && !needle) group.appendChild(treeNote(t('files.emptyDir'), depth + 1));
    else if (open && !kids && tree.loading.has(last.path)) {
      group.appendChild(treeNote(t('files.loading'), depth + 1));
    }
    if (group.childElementCount) node.appendChild(group);
    nodes.push(node);
  }
  if (entries.length > limit) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'lv-tree-note lv-tree-more';
    more.style.setProperty('--depth', String(depth));
    more.textContent = t('files.showMore', { n: entries.length - limit });
    more.addEventListener('click', () => {
      tree.shown.set(dir, limit + TREE_PAGE);
      renderList();
    });
    nodes.push(more);
  }
  // The server stopped at the workspace's listing cap: say the folder holds
  // more than it shows. Which entries made it is the directory's own order,
  // so this is "some of", not "the first".
  if (tree.cut.has(dir)) {
    const cut = treeNote(t('files.dirCut', { n: entries.length }), depth);
    cut.classList.add('lv-tree-cut');
    nodes.push(cut);
  }
  return nodes;
}

/**
 * The row of `path`: the one at that place in the tree (a file, or the folder
 * heading a chain), else one that ends a chain there or shows that file
 * through a link.
 */
const findRow = (path) => {
  const p = CSS.escape(path);
  return /** @type {HTMLElement | null} */ (
    els.list.querySelector(`.lv-file-row[data-head="${p}"]`) ||
      els.list.querySelector(`.lv-file-row[data-path="${p}"]`)
  );
};

/**
 * Show `path` in the tree: open the folders above it (and, `open`, the folder
 * itself), then bring its row into view. Narrow, that means leaving the file
 * for the tree; wide, the tree beside it moves and the file stays open.
 */
async function revealInTree(path, { open = false } = {}) {
  if (state.viewing && !isWide()) closeViewer();
  // Asked for by name, the row must show: a filter that might hide it goes.
  state.filter = '';
  if (els.filter) els.filter.value = '';
  syncTreeScope();
  for (const d of ancestorsOf(path)) tree.open.add(d);
  saveTree();
  await (state.mode === 'browse' ? refresh() : setMode('browse', { carry: true }));
  if (open) await openDir(path); // and the lone folders inside, as a click would
  // A row past its folder's first pages isn't rendered: each folder on the
  // way down shows enough pages to include the next step.
  for (const step of [...ancestorsOf(path), path]) {
    const dir = dirname(step);
    const i = tree.listings.get(dir)?.findIndex((e) => e.path === step) ?? -1;
    const need = Math.ceil((i + 1) / TREE_PAGE) * TREE_PAGE;
    if (need > (tree.shown.get(dir) || TREE_PAGE)) tree.shown.set(dir, need);
  }
  renderList();
  const row = findRow(path);
  if (!row) return;
  rove(row);
  row.scrollIntoView({ block: 'center' });
  row.classList.add('lv-flash'); // a re-render takes the flash with it, which is fine
  setTimeout(() => row.classList.remove('lv-flash'), 1200);
}

// ---- Keyboard: the list as one tab stop ------------------------------------
// Rows take focus one at a time (roving tabindex): Tab enters the list on the
// last row it left, and the arrows move within it — ↑↓ Home End everywhere,
// and in the tree → opens a folder or steps into it, ← closes it or steps up
// to its parent (the WAI-ARIA tree pattern). Enter opens, as a click does.
/** @param {HTMLElement | null | undefined} row */
function rove(row) {
  for (const r of els.list.querySelectorAll('.lv-file-row[tabindex="0"]')) {
    /** @type {HTMLElement} */ (r).tabIndex = -1;
  }
  if (row) row.tabIndex = 0;
}

/** @param {KeyboardEvent} e */
function listKeydown(e) {
  const row = e.target instanceof Element ? e.target.closest('.lv-file-row') : null;
  if (!row || e.altKey || e.ctrlKey || e.metaKey) return;
  const rows = /** @type {HTMLElement[]} */ ([...els.list.querySelectorAll('.lv-file-row')]);
  const i = rows.indexOf(/** @type {HTMLElement} */ (row));
  const expanded = row.getAttribute('aria-expanded');
  /** @param {Element | null | undefined} to */
  const go = (to) => {
    if (!(to instanceof HTMLElement)) return;
    e.preventDefault();
    to.focus();
    to.scrollIntoView({ block: 'nearest' });
  };
  switch (e.key) {
    case 'ArrowDown':
      return go(rows[i + 1]);
    case 'ArrowUp':
      return go(rows[i - 1]);
    case 'Home':
      return go(rows[0]);
    case 'End':
      return go(rows[rows.length - 1]);
    case 'ArrowRight':
      if (expanded === 'false') {
        e.preventDefault();
        /** @type {HTMLElement} */ (row).click();
      } else if (expanded === 'true') {
        go(row.nextElementSibling?.querySelector('.lv-file-row'));
      }
      return;
    case 'ArrowLeft':
      if (expanded === 'true') {
        e.preventDefault();
        /** @type {HTMLElement} */ (row).click();
      } else {
        // The row of the folder this one is in: its group's sibling.
        go(row.closest('.lv-tree-group')?.previousElementSibling);
      }
      return;
  }
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
 * Whether the panel's files are on another host: the chat's directory says,
 * else (no directories, or the agent's own) the agent's workspace does.
 */
function servesRemote() {
  const root = scopeRoot();
  if (root) return isRemote(root);
  return !!store.agents.find((a) => a.name === store.agent)?.remote;
}

/**
 * A row's right-click menu — what you'd otherwise open the file to reach:
 * attach it to the chat, copy its path, download it, and on this machine show
 * it in the file manager or open it in an editor. A folder offers its path
 * and the file manager; a file git says is deleted, its path.
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
  const local =
    entry.status === 'deleted'
      ? []
      : desktopItems({ path: entry.path, isDir: entry.is_dir, remote: servesRemote() });
  const tail = local.length ? [null, ...local] : [];
  if (entry.is_dir || entry.status === 'deleted') {
    openMenu(at, [copy, ...tail]);
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
    ...tail,
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

/**
 * One line per row: the name, then — in the change list — its folder, dimmed,
 * and what git says changed. Size, time and the rest wait in the tooltip.
 * `depth` makes it a tree row; a folder's `chain` (lone folders below it) is
 * one row, and `open` turns its chevron.
 * @param {any} entry
 * @param {{ depth?: number, chain?: any[] | null, open?: boolean, head?: string }} [opts]
 */
function rowFor(entry, { depth = -1, chain = null, open = false, head = entry.path } = {}) {
  const changes = state.mode === 'changes';
  const last = chain ? chain[chain.length - 1] : entry;
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'lv-file-row';
  row.tabIndex = -1; // renderList gives one row the list's tab stop
  row.dataset.path = last.path;
  // A chain grows and shrinks at its tail as listings come and go; its head
  // stays put, so the head is the row's identity across renders.
  row.dataset.head = head;
  // Browsing, a dot marks what this chat wrote; the change list groups it instead.
  if (!changes && !entry.is_dir && isTouched(entry.path)) row.classList.add('lv-touched');
  if (entry.status === 'deleted') row.classList.add('lv-deleted');
  if (state.viewing?.path === entry.path) row.classList.add('lv-active');

  if (depth >= 0) {
    row.classList.add('lv-tree-row');
    row.setAttribute('role', 'treeitem');
    row.setAttribute('aria-level', String(depth + 1));
    row.style.setProperty('--depth', String(depth));
    const twisty = document.createElement('span');
    twisty.className = 'lv-tree-twisty';
    if (entry.is_dir) {
      twisty.innerHTML = icon('chevron-right', { size: 12 });
      row.setAttribute('aria-expanded', String(open));
      if (open && depth < STICKY_DEPTH) row.classList.add('lv-sticky');
    }
    row.appendChild(twisty);
  }

  const ic = document.createElement('span');
  ic.className = 'lv-file-row-icon';
  ic.innerHTML = open ? icon('folder-open', { size: 15 }) : rowIcon(entry);

  const main = document.createElement('span');
  main.className = 'lv-file-row-main';
  const name = document.createElement('span');
  name.className = 'lv-file-row-name';
  name.textContent = (chain || [entry]).map((e) => basename(e.path)).join('/');
  main.appendChild(name);
  if (changes && dirname(entry.path)) {
    // Ellipsized from the left: the folder nearest the file is the telling one.
    const dir = document.createElement('span');
    dir.className = 'lv-file-row-dir';
    const bdi = document.createElement('bdi');
    bdi.textContent = dirname(entry.path);
    dir.appendChild(bdi);
    main.appendChild(dir);
  }
  row.append(ic, main);
  const mark = changes ? changeMark(entry) : null;
  if (mark) row.appendChild(mark);

  const facts = [];
  if (entry.old_path) facts.push(t('files.renamedFrom', { path: entry.old_path }));
  if (!entry.is_dir && entry.size != null) facts.push(formatBytes(entry.size));
  if (entry.mtime) facts.push(formatTimeSmart(entry.mtime));
  if (entry.symlink_target) facts.push('→ ' + entry.symlink_target);
  row.title = facts.length ? `${last.path}\n${facts.join(' · ')}` : last.path;

  row.addEventListener('contextmenu', (e) => rowMenu(last, e, row));
  row.addEventListener('click', () => {
    if (!entry.is_dir) {
      // From the change list a file opens on what changed; browsing, on itself.
      openFile(entry.path, { view: changes ? 'diff' : 'file' });
    } else if (depth >= 0) {
      toggleDir(chain || [entry]);
    } else {
      void revealInTree(entry.path, { open: true }); // an untracked folder in the change list
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
  // Git listed more than came back: the count is a floor.
  b.append(`${t('files.otherChanges')} · ${count}${state.truncated ? '+' : ''}`);
  b.addEventListener('click', () => {
    state.othersOpen = !state.othersOpen;
    renderList();
  });
  return b;
}

function renderList() {
  if (state.mode === 'background') return; // background.js's list
  if (state.failed) return; // the list is the failure and its retry until one works
  const frag = document.createDocumentFragment();
  const needle = state.filter.toLowerCase();
  if (state.mode === 'browse') renderTree(frag, needle);
  else renderChanges(frag, needle);
  // A re-render keeps the keyboard where it was: on the same row, by its head.
  const focused = /** @type {HTMLElement | null} */ (
    els.list.contains(document.activeElement) ? document.activeElement : null
  );
  const focusedHead = focused?.dataset.head;
  els.list.replaceChildren(frag);
  const again = focusedHead
    ? /** @type {HTMLElement | null} */ (
        els.list.querySelector(`.lv-file-row[data-head="${CSS.escape(focusedHead)}"]`)
      )
    : null;
  again?.focus({ preventScroll: true });
  rove(
    again ||
      els.list.querySelector('.lv-file-row.lv-active') ||
      els.list.querySelector('.lv-file-row'),
  );
  els.collapse?.classList.toggle('lv-hidden', state.mode !== 'browse' || !tree.open.size);
}

function renderTree(frag, needle) {
  syncTreeScope();
  if (!tree.listings.has('')) return; // on its way
  const nodes = treeLevel('', 0, needle);
  if (!nodes.length) frag.appendChild(emptyNode(needle ? t('files.noMatch') : t('files.emptyDir')));
  frag.append(...nodes);
}

/** "Nothing here yet" when nothing else is listed: what lands here, and how. */
function noFilesNode() {
  const div = document.createElement('div');
  div.className = 'lv-files-empty lv-files-empty-first';
  const title = document.createElement('div');
  title.className = 'lv-files-empty-title';
  title.textContent = t('files.noFiles');
  const hint = document.createElement('div');
  hint.textContent = t('files.noFilesHint');
  div.append(title, hint);
  return div;
}

function renderChanges(frag, needle) {
  const entries = needle
    ? state.entries.filter((e) => e.path.toLowerCase().includes(needle))
    : state.entries;
  // The change list leads with what THIS chat produced — the panel's whole
  // reason to exist for most visits — then, folded, what else git sees.
  const mine = entries.filter((e) => e.touched);
  const others = entries.filter((e) => !e.touched);
  if (!entries.length) {
    frag.appendChild(needle ? emptyNode(t('files.noMatch')) : noFilesNode());
  } else if (!mine.length && !needle) {
    // Others are listed: the chat's own group keeps its place, one quiet line.
    frag.appendChild(groupLabel(t('files.thisChat')));
    const none = document.createElement('div');
    none.className = 'lv-files-group-empty';
    none.textContent = t('files.noFilesShort');
    frag.appendChild(none);
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
  // Once per scope, whatever it finds: a later refresh (another tab's)
  // mustn't open what was saved since.
  if (_restoredScope === scope) return;
  _restoredScope = scope;
  if (state.viewing) return;
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(VIEWING_KEY) || 'null');
  } catch {
    /* unreadable: nothing to restore */
  }
  if (saved?.scope !== scope || typeof saved.path !== 'string') return;
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
  if (forget && !state.viewing?.guest) rememberViewing(null);
  dropGuest();
  state.viewing = null;
  els.viewer.classList.add('lv-hidden');
  els.panel.classList.remove('lv-viewing');
  els.viewerBody.replaceChildren();
  renderList(); // drop the active highlight
  store.emit('viewer-changed');
  // A narrow panel hid the list behind the file: back where the user left it.
  // (Wide, the list stayed in view and its scroll is the user's own.)
  if (!isWide()) listEl().scrollTop = state.listScroll || 0;
}

// ---- Guests ---------------------------------------------------------------------
// The viewer's place and frame, for something that is not a workspace file: a
// command's log. It keeps the viewer's name, wrap, copy, download and reader;
// what only a file has (its diff, its source view, attaching it) is off.

/**
 * @typedef {object} ViewerGuest
 * @property {string} key
 * @property {string} name
 * @property {string} title The full name, as the header's tooltip.
 * @property {string | null} download
 * @property {boolean} [wrap] Whether its lines can be wrapped or not (they can).
 * @property {{ label: string, text: string }} copy A "⋯" entry that copies `text`.
 * @property {HTMLElement} info A block between the header and the body.
 * @property {HTMLElement} content What the body holds, and the reader while it is open.
 * @property {() => string} text What Copy copies.
 * @property {() => void} mount `content` is in the body.
 * @property {() => void} unmount
 * @property {() => void} leaving `content` is about to move to or from the reader …
 * @property {() => void} moved … and has.
 */

/** @returns {ViewerGuest | null} */
export function viewedGuest() {
  return state.viewing?.guest ?? null;
}

/** @param {ViewerGuest} guest */
export function openGuest(guest) {
  coverList();
  dropGuest();
  rememberViewing(null); // a reload brings the file back, not the log
  state.viewing = { name: guest.name, guest };
  const name = document.createElement('span');
  name.className = 'lv-files-viewer-stem';
  name.textContent = guest.name;
  els.viewerName.replaceChildren(name);
  els.viewerName.title = guest.title;
  els.download.href = guest.download ?? '';
  els.download.classList.toggle('lv-hidden', !guest.download);
  syncViewerViews();
  els.viewerInfo.replaceChildren(guest.info);
  renderViewerContent();
  els.viewerBody.replaceChildren(guest.content);
  guest.mount();
  renderList(); // a file's row is no longer the open one
  store.emit('viewer-changed');
}

/**
 * Move a guest's content into the reader: the thing itself, not a copy as for
 * a file (syncModal) — it is still being written to. It moves back on close.
 * @param {ViewerGuest} guest
 */
function guestToReader(guest) {
  guest.leaving();
  const body = document.createElement('div');
  body.className = els.viewerBody.className;
  body.append(guest.content);
  els.modalBody.replaceChildren(body);
}

function dropGuest() {
  const guest = viewedGuest();
  if (!guest) return;
  guest.unmount();
  if (els.modal.open) els.modal.close();
  els.viewerInfo.replaceChildren();
}

/** Put the viewer up, over the list unless there is room beside it. */
function coverList() {
  if (!els.panel.classList.contains('lv-viewing')) state.listScroll = listEl().scrollTop;
  els.viewer.classList.remove('lv-hidden');
  els.panel.classList.add('lv-viewing');
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

// ---- Upload preview -------------------------------------------------------------
// A chat's upload, previewed over the chat the way an image opens in the
// lightbox — not in the panel, which is the workspace, and an upload is not in
// it. A dialog of its own, in the reader's look: the reader proper mirrors the
// panel's viewer. Read whole (a chat's folder has no paged read); past
// PREVIEW_MAX_BYTES, or not text (a NUL early on), it is a download.
const PREVIEW_MAX_BYTES = 2 * 1024 * 1024;

/** @param {{ name: string, url: string, download: string }} file */
async function previewUpload({ name, url, download }) {
  const button = (/** @type {string} */ tag, label, glyph) => {
    const b = document.createElement(tag);
    b.className = 'lv-btn-icon';
    b.title = label;
    b.setAttribute('aria-label', label);
    b.innerHTML = icon(glyph, { size: 16 });
    return b;
  };
  const dialog = document.createElement('dialog');
  dialog.className = 'lv-file-modal';
  dialog.setAttribute('aria-label', name);
  const head = document.createElement('div');
  head.className = 'lv-file-modal-head';
  const title = document.createElement('span');
  title.className = 'lv-file-modal-name';
  title.textContent = name;
  title.title = name;
  const dl = /** @type {HTMLAnchorElement} */ (button('a', t('files.download'), 'download'));
  dl.href = download;
  dl.setAttribute('download', '');
  const close = button('button', t('lightbox.close'), 'x');
  close.setAttribute('type', 'button');
  close.addEventListener('click', () => dialog.close());
  head.append(title, dl, close);
  const frame = document.createElement('div');
  frame.className = 'lv-file-modal-body';
  const body = document.createElement('div');
  body.className = 'lv-files-viewer-body';
  frame.appendChild(body);
  dialog.append(head, frame);
  dialog.addEventListener('click', (e) => {
    if (e.target === dialog) dialog.close(); // backdrop click
  });
  dialog.addEventListener('close', () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();

  const e = ext(name);
  if (e === 'pdf') {
    // The browser's own viewer, sandboxed from the page (as in the panel).
    const embed = document.createElement('embed');
    embed.className = 'lv-files-frame';
    embed.type = 'application/pdf';
    embed.src = url;
    body.replaceChildren(embed);
    return;
  }
  let text = null;
  try {
    const res = await fetch(download);
    if (!res.ok) throw await apiError(res);
    if (Number(res.headers.get('content-length')) > PREVIEW_MAX_BYTES) {
      void res.body?.cancel();
    } else {
      text = await res.text();
      if (text.length > PREVIEW_MAX_BYTES || text.slice(0, 1024).includes('\0')) text = null;
    }
  } catch (err) {
    body.replaceChildren(viewerNote(err?.message || t('files.readFailed')));
    return;
  }
  if (text === null) {
    const get = document.createElement('a');
    get.className = 'lv-btn lv-btn-ghost lv-btn-sm';
    get.textContent = t('files.download');
    get.href = download;
    get.setAttribute('download', '');
    body.replaceChildren(viewerNote(t('files.binary'), get));
    return;
  }
  if (MD_EXT.has(e)) {
    const { turn, body: md } = bodyWrapper();
    // A relative ref names another upload: they sit side by side, one folder,
    // no subfolders — so `diagram.png` is this URL's sibling, never a
    // workspace file of that name.
    const folder = url.slice(0, url.lastIndexOf('/') + 1);
    const refUrl = (/** @type {string} */ ref, /** @type {boolean} */ dl) => {
      const sibling = normalizeRel(ref);
      if (!sibling || sibling.includes('/') || ref.startsWith('/')) return null;
      return `${folder}${encodeURIComponent(sibling)}${dl ? '?download=1' : ''}`;
    };
    renderMarkdownInto(md, text, { refUrl });
    highlightIn(md);
    addCodeBlockControls(md);
    body.replaceChildren(turn);
    renderMermaid(md);
  } else if (HTML_EXT.has(e)) {
    body.replaceChildren(
      sandboxFrame(text, { title: name, className: 'lv-files-frame lv-files-frame--html' }),
    );
  } else {
    const table = CSV_EXT.has(e) ? renderCsv(text, e === 'tsv' ? '\t' : ',') : null;
    body.replaceChildren(table || renderText(text, name));
  }
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
  els.attach.classList.toggle('lv-hidden', deleted || !!v.guest);
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
const DIFF_META = /^(rename from|rename to|old mode|new mode|deleted file mode|new file mode) /;

function renderDiff(d, path) {
  if (d.binary) return viewerNote(t('files.diffBinary'));
  if (!d.diff.trim()) return viewerNote(t('files.diffEmpty'));
  const box = document.createElement('div');
  if (d.truncated) box.appendChild(viewerNote(t('files.diffCut')));
  const frame = document.createElement('div');
  frame.className = 'lv-files-diff'; // scrolls sideways; the rows inside share one width
  const lines = document.createElement('div');
  lines.className = 'lv-files-diff-rows';
  frame.appendChild(lines);
  const lang = diffLanguage(path, d.diff.length);
  const line = (cls, oldN, newN, text, isCode = false) => {
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
    if (isCode) code.append(text[0], diffSource(text.slice(1), lang));
    else code.textContent = text;
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
      line('lv-add', null, newN++, text, true);
    } else if (text[0] === '-') {
      line('lv-del', oldN++, null, text, true);
    } else {
      line('', oldN++, newN++, text, true);
    }
  }
  // Nothing git said is worth a row (a header shape not foreseen): say so,
  // rather than an empty box.
  if (!lines.childElementCount) return viewerNote(t('files.diffEmpty'));
  box.appendChild(frame);
  return box;
}

// Returns false when the file couldn't be read (so link-following can retry
// with a different base); true otherwise. `view` picks diff or file for a
// file that has both; a silent reopen (an agent write) keeps the one on screen.
async function openFile(path, { silent = false, view = undefined } = {}) {
  const name = basename(path);
  rememberViewing(path);
  coverList();
  if (viewedGuest()) {
    dropGuest();
    els.viewerBody.replaceChildren();
    state.viewing = null;
    store.emit('viewer-changed');
  }
  // The header names the file; its folder is in the tooltip, and "⋯" shows
  // it in the tree. A long name gives way in its middle, so the extension —
  // what kind of file this is — stays in view. An absolute path (outside the
  // workspace) shows whole, truncated from the left (rtl) to keep the name;
  // the bidi isolate keeps a leading "~/." from being reordered to the far end.
  if (path.startsWith('/')) {
    const abs = document.createElement('span');
    abs.className = 'lv-files-viewer-abs';
    const bdi = document.createElement('bdi');
    bdi.textContent = path;
    abs.appendChild(bdi);
    els.viewerName.replaceChildren(abs);
  } else {
    const dot = name.lastIndexOf('.');
    // A short tail after the last dot reads as an extension; anything else stays one piece.
    const cut = dot > 0 && name.length - dot <= 8 ? dot : name.length;
    const stem = document.createElement('span');
    stem.className = 'lv-files-viewer-stem';
    stem.textContent = name.slice(0, cut);
    const tail = document.createElement('span');
    tail.className = 'lv-files-viewer-ext';
    tail.textContent = name.slice(cut);
    els.viewerName.replaceChildren(stem, tail);
  }
  els.viewerName.title = path;
  els.download.href = api.workspaceRawUrl({ agent: store.agent, path, download: true });
  els.download.classList.remove('lv-hidden');
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
  if (!silent && state.mode !== 'changes') void refreshChangeMap();
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
  // A link in a task's report: the file opens on a file tab, and the task
  // waits on Background.
  if (state.mode === 'background') {
    if (!state.available) return; // no files to open
    void setMode(state.fileMode, { restore: false });
  }
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
/**
 * Mirror the viewer into the reader.
 * @param {{ keepScroll?: boolean }} [o] The same page, grown or re-sorted:
 *   the reader stays where it was read to (the copy is its scroller).
 */
function syncModal({ keepScroll = false } = {}) {
  const top = keepScroll ? (els.modalBody.firstElementChild?.scrollTop ?? 0) : 0;
  const copy = /** @type {HTMLElement} */ (els.viewerBody.cloneNode(true));
  copy.removeAttribute('id');
  els.modalBody.replaceChildren(copy);
  copy.scrollTop = top;
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
  els.viewerBody.classList.toggle('lv-nowrap', !state.wrap);
  if (v.guest) {
    els.mdToggle.classList.add('lv-hidden');
    els.wrapToggle.classList.toggle('lv-hidden', v.guest.wrap === false);
    els.copyContent.classList.remove('lv-hidden');
    return;
  }
  els.viewerBody.replaceChildren();
  if (v.view === 'diff') {
    if (!v.diff) return; // still loading
    els.mdToggle.classList.add('lv-hidden');
    els.wrapToggle.classList.toggle('lv-hidden', !v.diff.diff);
    els.copyContent.classList.toggle('lv-hidden', !v.diff.diff);
    els.viewerBody.appendChild(renderDiff(v.diff, v.path));
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
  if (v.kind === 'md' || v.kind === 'html') {
    els.mdToggle.classList.remove('lv-hidden');
    els.mdToggle.innerHTML = icon(v.raw ? 'file-text' : 'code', { size: 15 });
    els.mdToggle.title = v.raw ? t('files.rendered') : t('files.raw');
    els.mdToggle.setAttribute('aria-label', els.mdToggle.title);
  }
  appendFileBody(v);

  // Where the shown lines run out, so reading on is one click from there.
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
}

/**
 * The file itself, as its kind and the raw toggle say.
 * @param {NonNullable<typeof state.viewing>} v
 */
function appendFileBody(v) {
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

// A touched file that's on screen refreshes quietly, debounced — an agent
// making several quick edits shouldn't strobe the viewer.
let _reloadTimer = null;
function maybeReloadViewing(touchedPath) {
  const v = state.viewing;
  if (!v?.path) return;
  if (!(v.path === touchedPath || touchedPath.endsWith(`/${v.path}`))) return;
  clearTimeout(_reloadTimer);
  _reloadTimer = setTimeout(() => {
    if (state.viewing?.path !== v.path) return;
    openFile(v.path, { silent: true });
    // The write may have given the file a diff (Changes refreshes on its own).
    if (state.mode !== 'changes') void refreshChangeMap();
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
  els.tabBackground = document.getElementById('files-tab-background');
  els.changesStat = document.getElementById('files-changes-stat');
  els.refresh = document.getElementById('files-refresh');
  els.close = document.getElementById('files-close');
  els.collapse = document.getElementById('files-collapse');
  els.list = document.getElementById('files-list');
  els.background = document.getElementById('background-list');
  els.viewerInfo = document.getElementById('files-viewer-info');
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

  els.refresh.innerHTML = icon('refresh-cw', { size: 15 });
  if (els.collapse) {
    els.collapse.innerHTML = icon('chevrons-down-up', { size: 15 });
    els.collapse.addEventListener('click', () => {
      tree.open.clear();
      saveTree();
      renderList();
      els.list.scrollTop = 0;
    });
  }
  els.list.addEventListener('keydown', listKeydown);
  els.list.addEventListener('focusin', (e) => {
    const row = e.target instanceof Element ? e.target.closest('.lv-file-row') : null;
    if (row) rove(/** @type {HTMLElement} */ (row));
  });
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
  els.listToggle = document.getElementById('files-list-toggle');
  if (els.listToggle) {
    els.listToggle.innerHTML = icon('panel-right', { size: 15 });
    syncListToggle();
    els.listToggle.addEventListener('click', toggleList);
  }
  els.viewerBack = document.getElementById('files-viewer-back');
  if (els.viewerBack) {
    els.viewerBack.innerHTML = icon('chevron-left', { size: 16 });
    els.viewerBack.addEventListener('click', () => closeViewer());
  }
  // "⋯" holds what the header has no room for — a narrow panel folds the
  // source toggle, line wrap, attach and download into it (CSS decides when)
  // — then the file's place: show it in the tree, copy its path. A folded
  // item clicks the real button, so its handler and its hidden state (no
  // wrap for an image) are the button's own.
  els.more = document.getElementById('files-more');
  if (els.more) {
    els.more.innerHTML = icon('ellipsis', { size: 15 });
    els.more.addEventListener('click', () => {
      const v = state.viewing;
      if (!v) return;
      // Folded away by the container query — not merely hidden for this file.
      const folded = (/** @type {HTMLElement} */ b) =>
        !b.classList.contains('lv-hidden') && !b.getClientRects().length;
      /** @type {(import('./ui.js').MenuItem | null)[]} */
      const items = [];
      if (folded(els.mdToggle)) {
        items.push({
          label: els.mdToggle.title,
          icon: icon(v.raw ? 'file-text' : 'code', { size: 15 }),
          onSelect: () => els.mdToggle.click(),
        });
      }
      if (folded(els.wrapToggle)) {
        items.push({
          label: t('files.wrap'),
          icon: icon('wrap-text', { size: 15 }),
          checked: state.wrap,
          onSelect: () => els.wrapToggle.click(),
        });
      }
      if (folded(els.attach)) {
        items.push({ label: t('files.attach'), icon: icon('paperclip', { size: 15 }), onSelect: () => els.attach.click() });
      }
      if (folded(els.download)) {
        items.push({ label: t('files.download'), icon: icon('download', { size: 15 }), onSelect: () => els.download.click() });
      }
      if (items.length) items.push(null);
      if (v.guest) {
        const { label, text } = v.guest.copy;
        items.push({
          label,
          icon: icon('copy', { size: 15 }),
          onSelect: async () => {
            if (await copyToClipboard(text)) toast(t('toast.contentCopied'));
          },
        });
        openMenu(els.more, items);
        return;
      }
      if (!v.path.startsWith('/')) {
        items.push({
          label: t('files.revealInTree'),
          icon: icon('folder-open', { size: 15 }),
          onSelect: () => void revealInTree(v.path),
        });
        items.push(...desktopItems({ path: v.path, isDir: false, remote: servesRemote() }));
      }
      items.push({
        label: t('files.copyPath'),
        icon: icon('copy', { size: 15 }),
        onSelect: async () => {
          if (await copyToClipboard(v.path)) toast(t('ws.pathCopied'));
        },
      });
      openMenu(els.more, items);
    });
  }

  if (els.resizer) initResizer();
  initLayout();

  // The Files button is for the file tabs, as the topbar's background button
  // is for that one: each shows its own, and closes the panel showing it.
  els.btn.addEventListener('click', () => {
    if (filesShown()) return setOpen(false);
    if (state.mode === 'background') void setMode(state.fileMode);
    if (!state.open) setOpen(true);
  });
  els.close.addEventListener('click', () => setOpen(false));
  els.refresh.addEventListener('click', () => {
    if (state.mode === 'background') store.emit('panel-changed'); // lists anew
    else refresh();
  });
  els.tabChanges.addEventListener('click', () => setMode('changes'));
  els.tabBrowse.addEventListener('click', () => setMode('browse'));
  els.tabBackground.addEventListener('click', () => setMode('background'));
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
    const content = v?.guest ? v.guest.text() : v?.view === 'diff' ? v.diff?.diff : v?.content;
    if (typeof content !== 'string') return;
    if (await copyToClipboard(content)) toast(t('toast.contentCopied'));
  });
  els.fullscreen.addEventListener('click', () => {
    const v = state.viewing;
    if (!v) return;
    els.modalName.textContent = v.name || v.path;
    if (v.guest) guestToReader(v.guest);
    else syncModal();
    els.modal.showModal();
    v.guest?.moved();
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
      syncModal({ keepScroll: true });
      return;
    }
    const copyBtn = target?.closest('.lv-btn-copy-code');
    if (copyBtn) {
      void copyCodeBlock(copyBtn);
      return;
    }
    if (target?.closest('.lv-files-load-more')) {
      await loadMore();
      syncModal({ keepScroll: true });
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
  els.modal.addEventListener('close', () => {
    const guest = viewedGuest();
    if (guest && els.modalBody.contains(guest.content)) {
      guest.leaving();
      els.viewerBody.append(guest.content);
      guest.moved();
    }
    els.modalBody.replaceChildren();
  });
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
    parked.changes = parked.browse = null;
    state.touched.clear();
    state.revs.clear();
    state.filter = '';
    if (els.filter) els.filter.value = '';
    clearUnseen();
    closeViewer({ forget: false });
    clearList();
    updateVisibility();
    refresh();
  });
  // The panel shows the directory the chat on screen works in (a draft's,
  // for a new chat): a different one is a different tree — start over.
  // (The tree follows the scope on its own: see syncTreeScope.)
  store.on('workspace-scope-changed', () => {
    parked.changes = parked.browse = null;
    state.filter = '';
    if (els.filter) els.filter.value = '';
    closeViewer({ forget: false });
    clearList();
    if (state.open) refresh();
  });
  // "touched" (and the edit revisions) are scoped to the chat on screen, and
  // so is a guest in the viewer. The list drops at once what the chat just
  // left wrote; the replay of the new one's history asks for its own.
  const chatChanged = () => {
    if (viewedGuest()) closeViewer();
    parked.background = null; // the last chat's log or task
    state.touched.clear();
    state.revs.clear();
    clearUnseen();
    state.entries = state.entries.filter((e) => !e.touched);
    queueRender();
    queueRefresh();
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
    if (filesShown() || !store.streaming) return;
    const agent = store.agent;
    const scratch = await scratchDirFor(agent);
    if (store.agent !== agent || filesShown() || isScratch(key, scratch)) return;
    state.unseen.add(key);
    renderBadge();
  });
  store.on('workspace-maybe-stale', () => {
    state.stale = true;
    if (filesShown()) els.refresh.classList.add('lv-stale');
  });
  // A tool card's "open in Files panel" action (chat.js) — open the panel and
  // jump straight to the file the tool touched.
  // A message's uploaded file (chat.js): a preview over the chat.
  store.on('preview-upload', (file) => void previewUpload(file));
  store.on('open-workspace-file', async ({ path }) => {
    if (!state.available) {
      toast(t('files.noWorkspace'));
      return;
    }
    // A file opens on a file tab, never beside the Background list.
    if (state.mode === 'background') void setMode(state.fileMode, { restore: false });
    if (!state.open) setOpen(true);
    if (!(await openFile(path))) {
      toast(t('files.openFailed'), { type: 'error' });
    }
  });
}
