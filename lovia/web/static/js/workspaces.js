// workspaces.js — the directories chats work in (create_app(workspaces=),
// /api/info features.workspace_switch).
//
// A chat is bound to a directory when it is created and keeps it. A new chat
// takes the directory this tab has picked: the composer's workspace chip, a
// sidebar group's "+", or the chat last opened here (each tab remembers its
// own — sessionStorage). Attachments uploaded to a draft land in its
// directory, so the chip locks while the draft holds any.
//
// This module owns the list (GET /api/workspaces), the composer chip, the
// workspace scope every Files-panel / upload request carries (api.js), and
// the dialogs that add a directory or link chats from before chats had one.
// sessions.js draws the sidebar groups from sidebarGroups().
import { api, setWorkspaceScope } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { confirmDialog, openMenu, showDialog } from './ui.js';

const PICK_KEY = 'lovia-workspace'; // sessionStorage: this tab's pick
const COLLAPSED_KEY = 'lovia-workspace-collapsed'; // localStorage: folded groups

/**
 * @typedef {{ id: string | null, root: string, name: string,
 *   kind: 'default' | 'general' | 'added', last_used_at: number | null }} Workspace
 * @typedef {{ workspaces: Workspace[], default: string | null,
 *   can_add: boolean, roots: string[] | null }} WorkspaceList
 */

/** @type {WorkspaceList | null} */
let _list = null;
/** This tab's pick: the directory a new chat starts in. */
let _picked = /** @type {string | null} */ (null);
/** Directories of chats the list doesn't carry (task chats), from their detail. */
const _rootOf = new Map();
/** @type {HTMLButtonElement | null} */
let _chip = null;
/** The draft holds attachments (uploaded into its directory). */
let _draftHasFiles = false;
/** The directory the workspace routes last served (for change events). */
let _scopeRoot = /** @type {string | null | undefined} */ (undefined);

// ---- state ----------------------------------------------------------------

/** Whether chats on this server work in directories of their own. */
export function workspacesOn() {
  return _list !== null;
}

/** Whether directories beyond the fixed ones may be opened. */
export function canAddWorkspace() {
  return !!_list?.can_add;
}

/** @param {string | null | undefined} root */
function entry(root) {
  return root ? _list?.workspaces.find((w) => w.root === root) : undefined;
}

/**
 * A directory's display name: the list's, "General" for the catch-all,
 * else its last path segment.
 * @param {string | null | undefined} root
 */
export function workspaceName(root) {
  if (!root) return t('ws.unassociated');
  const w = entry(root);
  if (w?.kind === 'general') return t('ws.general');
  return w?.name || root.split('/').filter(Boolean).pop() || root;
}

/** @param {string | null | undefined} id */
function sessionRow(id) {
  return id ? store.sessions.find((s) => s.id === id) : undefined;
}

/**
 * The open chat's directory: null when it has none yet, undefined when not
 * known (a chat whose detail hasn't loaded).
 * @param {string | null} id
 */
function chatRoot(id) {
  if (!id) return undefined;
  const row = sessionRow(id);
  if (row) return row.workspace_root ?? null;
  return _rootOf.has(id) ? _rootOf.get(id) : undefined;
}

/**
 * Whether `row` (a chat) must be linked to a directory before it continues:
 * it predates directories, on a server where chats may work elsewhere.
 * @param {{ workspace_root?: string | null, parent_id?: string | null } | undefined} row
 */
export function needsLink(row) {
  return !!(_list?.can_add && row && row.workspace_root == null);
}

/** The directory a new chat starts in: this tab's pick, if still offered. */
function draftRoot() {
  if (_picked && entry(_picked)) return _picked;
  return _list?.default ?? null;
}

/**
 * The directory a new chat's first message asks for; undefined for an
 * existing chat (its own applies) or a server without directories.
 */
export function draftWorkspaceRoot() {
  return !store.sessionId && _list ? draftRoot() ?? undefined : undefined;
}

/**
 * The directory the workspace routes serve now (an upload lands there);
 * undefined without directories, or for a chat not known yet.
 */
export function scopeRoot() {
  return _list ? _scopeRoot : undefined;
}

/** @param {string | null} root */
function pick(root) {
  _picked = root;
  try {
    if (root) sessionStorage.setItem(PICK_KEY, root);
  } catch {
    /* storage off: the pick lasts for this page */
  }
  renderChip();
  syncScope();
}

/**
 * The directory the workspace routes serve now — a chat with none yet is
 * served the agent's own — and announce a change (undefined: not known yet).
 */
function syncScope() {
  const id = store.sessionId;
  const known = id ? chatRoot(id) : draftRoot();
  const root = known === null ? _list?.default ?? null : known;
  if (root === _scopeRoot) return;
  _scopeRoot = root;
  store.emit('workspace-scope-changed', root);
}

// ---- sidebar groups ---------------------------------------------------------

/** @returns {Set<string>} */
function collapsed() {
  try {
    return new Set(JSON.parse(localStorage.getItem(COLLAPSED_KEY) || '[]'));
  } catch {
    return new Set();
  }
}

/** @param {string} key @param {boolean} fold */
function setCollapsed(key, fold) {
  const keys = collapsed();
  if (fold) keys.add(key);
  else keys.delete(key);
  try {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...keys]));
  } catch {
    /* storage off: folding lasts for this page */
  }
}

/**
 * @typedef {{ key: string, root: string | null, label: string,
 *   kind: 'default' | 'general' | 'added' | 'unlinked', workspace?: Workspace,
 *   chats: any[], collapsed: boolean }} SidebarGroup
 */

/**
 * The sidebar's workspace groups for `chats` (top-level, in server order),
 * or null when grouping says nothing — one fixed directory. Every listed
 * directory gets a group (an empty one is where a new chat starts); a chat
 * whose directory was removed from the list is left out (search finds it);
 * chats with none yet gather under "Not linked", last.
 * @param {any[]} chats
 * @returns {SidebarGroup[] | null}
 */
export function sidebarGroups(chats) {
  if (!_list) return null;
  // Where each chat runs: one with no directory yet, in the agent's own when
  // no other may be opened.
  const fallback = _list.can_add ? null : _list.default;
  const roots = new Set(chats.map((s) => s.workspace_root ?? fallback));
  if (!_list.can_add && roots.size <= 1) return null;
  const folded = collapsed();
  /** @type {Map<string, SidebarGroup>} */
  const groups = new Map();
  for (const w of _list.workspaces) {
    groups.set(w.root, {
      key: w.root,
      root: w.root,
      label: workspaceName(w.root),
      kind: w.kind,
      workspace: w,
      chats: [],
      collapsed: folded.has(w.root),
    });
  }
  /** @type {any[]} */
  const unlinked = [];
  for (const s of chats) {
    const root = s.workspace_root ?? null;
    if (root === null) {
      if (_list.can_add) unlinked.push(s);
      else groups.get(_list.default ?? '')?.chats.push(s); // runs in the agent's own
      continue;
    }
    groups.get(root)?.chats.push(s);
  }
  // The launch directory first; the rest by their latest activity, so the
  // project being worked on rises.
  const activity = (/** @type {SidebarGroup} */ g) =>
    g.kind === 'default'
      ? Infinity
      : Math.max(g.workspace?.last_used_at || 0, ...g.chats.map((s) => s.updated_at || 0));
  const ordered = [...groups.values()].sort((a, b) => activity(b) - activity(a));
  if (unlinked.length) {
    ordered.push({
      key: '\u0000unlinked',
      root: null,
      label: t('ws.unassociated'),
      kind: 'unlinked',
      chats: unlinked,
      collapsed: folded.has('\u0000unlinked'),
    });
  }
  return ordered;
}

/**
 * A group's header: fold toggle, name (full path on hover), and its actions —
 * a new chat there, and a menu (remove from the list), or "Link…" for the
 * chats that have no directory yet.
 * @param {SidebarGroup} group
 * @param {() => void} rerender
 */
export function groupHeader(group, rerender) {
  const head = document.createElement('div');
  head.className = 'lv-ws-group-head';
  head.dataset.kind = group.kind;
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'lv-ws-group-toggle';
  toggle.setAttribute('aria-expanded', String(!group.collapsed));
  toggle.title =
    group.kind === 'unlinked'
      ? t('ws.unassociatedTitle')
      : group.kind === 'general'
        ? t('ws.generalTitle', { path: group.root || '' })
        : group.root || '';
  const glyph = group.kind === 'unlinked' ? 'triangle-alert' : 'folder';
  toggle.innerHTML =
    `<span class="lv-ws-group-icon" aria-hidden="true">${icon(glyph, { size: 15 })}</span>` +
    `<span class="lv-ws-group-chevron" aria-hidden="true">${icon('chevron-right', { size: 13 })}</span>`;
  const name = document.createElement('span');
  name.className = 'lv-ws-group-name';
  name.textContent = group.label;
  toggle.appendChild(name);
  toggle.addEventListener('click', () => {
    setCollapsed(group.key, !group.collapsed);
    rerender();
  });
  head.appendChild(toggle);

  const actions = document.createElement('div');
  actions.className = 'lv-ws-group-actions';
  if (group.kind === 'unlinked') {
    const link = document.createElement('button');
    link.type = 'button';
    link.className = 'lv-ws-group-link';
    link.textContent = t('ws.link');
    link.addEventListener('click', () => linkChats(group.chats.map((s) => s.id)));
    actions.appendChild(link);
  } else {
    const root = /** @type {string} */ (group.root);
    if (group.workspace?.id) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'lv-btn-icon lv-ws-group-btn';
      more.title = more.ariaLabel = t('session.actions');
      more.setAttribute('aria-haspopup', 'menu');
      more.setAttribute('aria-expanded', 'false');
      more.innerHTML = icon('ellipsis', { size: 15 });
      more.addEventListener('click', () =>
        openMenu(more, [
          { label: t('ws.newChatIn', { name: group.label }), icon: icon('square-pen', { size: 15 }), onSelect: () => newChatIn(root) },
          null,
          { label: t('ws.remove'), icon: icon('x', { size: 15 }), danger: true, onSelect: () => removeWorkspace(group) },
        ]),
      );
      actions.appendChild(more);
    }
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'lv-btn-icon lv-ws-group-btn';
    add.title = add.ariaLabel = t('ws.newChatIn', { name: group.label });
    add.innerHTML = icon('square-pen', { size: 15 });
    add.addEventListener('click', () => newChatIn(root));
    actions.appendChild(add);
  }
  head.appendChild(actions);
  return head;
}

/** @param {string} root */
function newChatIn(root) {
  pick(root);
  store.emit('new-chat-request');
}

/** @param {SidebarGroup} group */
async function removeWorkspace(group) {
  const id = group.workspace?.id;
  if (!id || !(await confirmDialog(t('ws.removeConfirm', { name: group.label })))) return;
  try {
    await api.removeWorkspace(id);
  } catch (err) {
    toast(String(/** @type {Error} */ (err).message || err), { type: 'error' });
    return;
  }
  await reloadList();
}

/** Refetch the list and redraw what shows it. */
export async function reloadList() {
  try {
    _list = await api.listWorkspaces();
  } catch {
    return; // transient: the next change retries
  }
  renderChip();
  syncScope();
  store.emit('workspaces-changed');
}

// ---- the composer chip ------------------------------------------------------

function renderChip() {
  if (!_chip || !_list) return;
  const id = store.sessionId;
  const root = id ? chatRoot(id) : draftRoot();
  const label = /** @type {HTMLElement} */ (_chip.querySelector('.lv-ws-chip-label'));
  const glyph = /** @type {HTMLElement} */ (_chip.querySelector('.lv-ws-chip-icon'));
  _chip.hidden = !!id && root === undefined; // not known yet
  _chip.classList.toggle('lv-unlinked', !!id && root === null && !!_list.can_add);
  _chip.classList.toggle('lv-fixed', !!id && !!root);
  const unlinked = _chip.classList.contains('lv-unlinked');
  glyph.innerHTML = icon(unlinked ? 'triangle-alert' : 'folder', { size: 14 });
  label.textContent = root ? workspaceName(root) : t('ws.unassociated');
  const locked = !id && _draftHasFiles;
  _chip.setAttribute('aria-disabled', String(locked));
  _chip.title = unlinked
    ? t('ws.chipUnassociated')
    : locked
      ? t('ws.chipLocked')
      : id
        ? t('ws.chipFixed', { path: root || '' })
        : t('ws.chipTitle', { path: root || '' });
}

function openChipMenu() {
  if (!_chip || !_list) return;
  const id = store.sessionId;
  if (id) {
    const root = chatRoot(id);
    if (root === null && _list.can_add) {
      linkChats([id]);
      return;
    }
    if (!root) return;
    openMenu(
      _chip,
      [{ label: t('ws.newChatIn', { name: workspaceName(root) }), icon: icon('square-pen', { size: 15 }), onSelect: () => newChatIn(root) }],
      { align: 'start' },
    );
    return;
  }
  // The first message is out and the chat not created yet: it went with
  // the draft's directory, so a pick now would be silently lost.
  if (store.streaming) return;
  if (_draftHasFiles) {
    toast(t('ws.chipLocked'));
    return;
  }
  const current = draftRoot();
  // In the sidebar's order: where the person last worked comes first.
  const groups = sidebarGroups(store.sessions.filter((s) => !s.parent_id)) || [];
  const listed = _list.workspaces;
  const ordered = [
    ...groups.map((g) => g.workspace).filter((w) => !!w),
    ...listed.filter((w) => !groups.some((g) => g.root === w.root)),
  ];
  /** @type {(import('./ui.js').MenuItem | null)[]} */
  const items = /** @type {Workspace[]} */ (ordered).map((w) => ({
    label: workspaceName(w.root),
    icon: icon(w.root === current ? 'check' : 'folder', { size: 15 }),
    onSelect: () => pick(w.root),
  }));
  if (_list.can_add) {
    items.push(null, {
      label: t('ws.add'),
      icon: icon('plus', { size: 15 }),
      onSelect: async () => {
        const added = await addWorkspaceDialog();
        if (added) pick(added.root);
      },
    });
  }
  openMenu(_chip, items, { align: 'start' });
}

/**
 * Before a message goes to the open chat: a chat with no directory yet is
 * linked to one first. Resolves false when the person backed out.
 */
export async function ensureLinked() {
  const id = store.sessionId;
  if (!id || !_list?.can_add || chatRoot(id) !== null) return true;
  return linkChats([id]);
}

// ---- dialogs ---------------------------------------------------------------

/**
 * A path input that completes directories as you type (Tab or Enter takes
 * the highlighted one).
 * @param {(path: string) => void} onSubmit
 */
function pathInput(onSubmit) {
  const wrap = document.createElement('div');
  wrap.className = 'lv-ws-path';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'lv-dialog-input';
  input.placeholder = t('ws.pathPlaceholder');
  input.spellcheck = false;
  input.autocomplete = 'off';
  input.setAttribute('autocapitalize', 'off');
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-expanded', 'false');
  const list = document.createElement('div');
  list.className = 'lv-ws-path-suggestions';
  list.setAttribute('role', 'listbox');
  list.hidden = true;
  wrap.append(input, list);

  /** @type {string[]} */
  let options = [];
  let active = -1;
  /** @type {AbortController | null} */
  let inflight = null;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let timer;

  const draw = () => {
    list.replaceChildren();
    list.hidden = !options.length;
    input.setAttribute('aria-expanded', String(!list.hidden));
    options.forEach((path, i) => {
      const opt = document.createElement('div');
      opt.className = 'lv-ws-path-option' + (i === active ? ' lv-active' : '');
      opt.setAttribute('role', 'option');
      opt.setAttribute('aria-selected', String(i === active));
      // The folder's own name: the rest is what's already typed.
      opt.textContent = `${path.split('/').filter(Boolean).pop() || path}/`;
      opt.title = path;
      opt.addEventListener('pointerdown', (e) => {
        e.preventDefault(); // keep focus in the input
        take(path);
      });
      list.appendChild(opt);
    });
  };
  const fetchNow = async () => {
    inflight?.abort();
    const ctrl = (inflight = new AbortController());
    try {
      // Empty: what's in the home directory, without typing it in for them.
      const found = await api.completeDir(input.value || '~/', { signal: ctrl.signal });
      if (ctrl.signal.aborted) return;
      options = Array.isArray(found) ? found : [];
      active = -1;
      draw();
    } catch {
      /* aborted or offline: keep what shows */
    }
  };
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(fetchNow, 120);
  };
  /** @param {string} path */
  const take = (path) => {
    input.value = path.endsWith('/') ? path : `${path}/`;
    options = [];
    draw();
    input.focus();
    schedule(); // straight on to its children
  };
  input.addEventListener('input', schedule);
  input.addEventListener('focus', () => {
    if (!input.value) schedule();
  });
  input.addEventListener('blur', () => {
    setTimeout(() => {
      options = [];
      draw();
    }, 100);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!options.length) return;
      e.preventDefault();
      const n = options.length;
      active = (active + (e.key === 'ArrowDown' ? 1 : n - 1)) % n;
      draw();
    } else if (e.key === 'Tab' && options.length) {
      e.preventDefault();
      take(options[Math.max(active, 0)]);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (active >= 0 && options[active]) take(options[active]);
      else onSubmit(input.value.trim());
    } else if (e.key === 'Escape' && options.length) {
      e.preventDefault();
      e.stopPropagation(); // the suggestions, not the dialog
      options = [];
      draw();
    }
  });
  return { wrap, input };
}

/**
 * What went wrong with a directory, in the page's language where the server
 * said which rule; its own words (with the constraint) otherwise.
 * @param {any} err
 */
function folderError(err) {
  if (err?.code === 'file_not_found' || err?.code === 'workspace_missing') return t('ws.errMissing');
  if (err?.code === 'invalid_request' && err?.status === 400) return t('ws.errAbsolute');
  if (err?.code === 'workspace_not_allowed') {
    return t('ws.errNotAllowed', { why: err.hint || '' });
  }
  return String(err?.message || err);
}

/** @param {string} text */
function para(text, className = '') {
  const p = document.createElement('p');
  p.className = className;
  p.textContent = text;
  return p;
}

/**
 * The "Add a workspace" dialog. Resolves to the added entry, or null.
 * @returns {Promise<Workspace | null>}
 */
export function addWorkspaceDialog() {
  return new Promise((resolve) => {
    /** @type {Workspace | null} */
    let added = null;
    const body = document.createElement('div');
    body.className = 'lv-ws-dialog';
    const title = document.createElement('h3');
    title.textContent = t('ws.addTitle');
    const error = para('', 'lv-ws-dialog-error');
    error.hidden = true;
    const submit = async (/** @type {string} */ path) => {
      if (!path) return;
      okBtn.disabled = true;
      try {
        added = await api.addWorkspace(path);
      } catch (err) {
        error.textContent = folderError(err);
        error.hidden = false;
        okBtn.disabled = false;
        return;
      }
      await reloadList();
      toast(t('ws.added', { name: workspaceName(added?.root) }));
      dialog.close('ok');
    };
    const { wrap, input } = pathInput((path) => void submit(path));
    input.addEventListener('input', () => (error.hidden = true));
    body.append(title, para(t('ws.addHint'), 'lv-ws-dialog-hint'), wrap, error);

    const actions = document.createElement('div');
    actions.className = 'lv-ws-dialog-actions';
    const cancel = document.createElement('button');
    cancel.className = 'lv-btn lv-btn-ghost';
    cancel.textContent = t('dialog.cancel');
    cancel.addEventListener('click', () => dialog.close(''));
    const okBtn = document.createElement('button');
    okBtn.className = 'lv-btn lv-btn-primary';
    okBtn.textContent = t('ws.addBtn');
    okBtn.addEventListener('click', () => void submit(input.value.trim()));
    actions.append(cancel, okBtn);
    const dialog = showDialog({ body, actions, stack: true, onClose: () => resolve(added) });
    setTimeout(() => input.focus(), 50);
  });
}

/**
 * Directories approvals in `id` were given under: where it most likely ran.
 * @param {string} id
 * @returns {Promise<string[]>}
 */
async function grantedRoots(id) {
  try {
    const grants = await api.listGrants(id);
    /** @type {Map<string, number>} */
    const seen = new Map();
    for (const g of grants) {
      const root = JSON.parse(g.context)?.[1];
      if (typeof root === 'string') seen.set(root, (seen.get(root) || 0) + 1);
    }
    return [...seen].sort((a, b) => b[1] - a[1]).map(([root]) => root);
  } catch {
    return [];
  }
}

/**
 * Link chats that predate directories to one, picked in a dialog — the
 * directories approvals were given in first, as a hint, never a default.
 * Resolves true once linked.
 * @param {string[]} ids
 * @returns {Promise<boolean>}
 */
export function linkChats(ids) {
  return new Promise((resolve) => {
    if (!_list || !ids.length) return resolve(false);
    let linked = false;
    const body = document.createElement('div');
    body.className = 'lv-ws-dialog';
    const title = document.createElement('h3');
    title.textContent = t('ws.associateTitle');
    body.append(
      title,
      para(
        ids.length === 1 ? t('ws.associateOne') : t('ws.associateMany', { n: ids.length }),
        'lv-ws-dialog-hint',
      ),
    );
    const choices = document.createElement('div');
    choices.className = 'lv-ws-choices';
    choices.setAttribute('role', 'radiogroup');
    body.appendChild(choices);
    const error = para('', 'lv-ws-dialog-error');
    error.hidden = true;

    const name = `ws-link-${Date.now()}`;
    /** @param {string} value @param {string} label @param {string} [sub] */
    const choice = (value, label, sub) => {
      const row = document.createElement('label');
      row.className = 'lv-ws-choice';
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = name;
      radio.value = value;
      const text = document.createElement('span');
      text.className = 'lv-ws-choice-text';
      const strong = document.createElement('span');
      strong.className = 'lv-ws-choice-name';
      strong.textContent = label;
      text.appendChild(strong);
      if (sub) {
        const small = document.createElement('span');
        small.className = 'lv-ws-choice-sub';
        small.title = sub;
        // Isolated left-to-right inside the right-to-left box that cuts the
        // path at its start: the characters keep their order.
        const bdi = document.createElement('bdi');
        bdi.dir = 'ltr';
        bdi.textContent = sub;
        small.appendChild(bdi);
        text.appendChild(small);
      }
      row.append(radio, text);
      radio.addEventListener('change', () => {
        error.hidden = true;
        other.wrap.hidden = value !== '\u0000other';
        if (!other.wrap.hidden) other.input.focus();
      });
      choices.appendChild(row);
      return radio;
    };
    const draw = (/** @type {string[]} */ hinted) => {
      choices.replaceChildren();
      const listed = _list?.workspaces || [];
      const order = [...hinted, ...listed.map((w) => w.root).filter((r) => !hinted.includes(r))];
      for (const root of order) {
        const sub = hinted.includes(root) ? `${root} · ${t('ws.recommended')}` : root;
        choice(root, workspaceName(root), sub);
      }
      if (_list?.can_add) choice('\u0000other', t('ws.other'));
    };
    const submit = async () => {
      const checked = /** @type {HTMLInputElement | null} */ (
        choices.querySelector(`input[name="${name}"]:checked`)
      );
      const root = checked?.value === '\u0000other' ? other.input.value.trim() : checked?.value;
      if (!root) {
        error.textContent = t('ws.pickOne');
        error.hidden = false;
        return;
      }
      okBtn.disabled = true;
      let answer;
      try {
        answer = await api.associateSessions(ids, root);
      } catch (err) {
        error.textContent = folderError(err);
        error.hidden = false;
        okBtn.disabled = false;
        return;
      }
      for (const sid of answer.associated || []) noteRoot(sid, answer.root);
      linked = true;
      await reloadList();
      toast(t('ws.associated', { name: workspaceName(answer.root) }));
      dialog.close('ok');
    };
    const other = pathInput(() => void submit());
    other.wrap.hidden = true;
    other.wrap.classList.add('lv-ws-choice-other');
    draw([]);
    // Hinted by where one chat's approvals were given — fetched after the
    // dialog opens, so it never waits on them.
    if (ids.length === 1) {
      void grantedRoots(ids[0]).then((roots) => {
        if (roots.length && dialog.open && !choices.querySelector(':checked')) draw(roots);
      });
    }
    // Under the list, not in it: its suggestions mustn't scroll with it.
    body.append(other.wrap, error);

    const actions = document.createElement('div');
    actions.className = 'lv-ws-dialog-actions';
    const cancel = document.createElement('button');
    cancel.className = 'lv-btn lv-btn-ghost';
    cancel.textContent = t('dialog.cancel');
    cancel.addEventListener('click', () => dialog.close(''));
    const okBtn = document.createElement('button');
    okBtn.className = 'lv-btn lv-btn-primary';
    okBtn.textContent = t('ws.associateBtn');
    okBtn.addEventListener('click', () => void submit());
    actions.append(cancel, okBtn);
    const dialog = showDialog({ body, actions, onClose: () => resolve(linked) });
    // Focus the first choice, not the scrollable list around it (which the
    // browser would otherwise pick, and ring).
    /** @type {HTMLElement | null} */ (choices.querySelector('input'))?.focus();
  });
}

/**
 * Record a chat's directory wherever this page keeps it.
 * @param {string} id @param {string | null} root
 */
function noteRoot(id, root) {
  _rootOf.set(id, root);
  const row = sessionRow(id);
  if (row) row.workspace_root = root;
  if (id === store.sessionId) {
    if (root) pick(root);
    renderChip();
    syncScope();
  }
}

// ---- boot ------------------------------------------------------------------

/**
 * Boot: fetch the list, mount the composer chip, and scope the workspace
 * routes. Called from main.js once /api/info resolved; a no-op on a server
 * whose chats don't pick a directory.
 */
export async function initWorkspaces() {
  if (_list || !store.serverInfo?.features?.workspace_switch) return;
  try {
    _list = await api.listWorkspaces();
  } catch {
    return; // e.g. the CLI before a model is configured: nothing to pick yet
  }
  try {
    _picked = sessionStorage.getItem(PICK_KEY);
  } catch {
    _picked = null;
  }
  setWorkspaceScope(() => {
    const id = store.sessionId;
    if (id) return { session: id };
    const root = draftRoot();
    return root ? { root } : {};
  });

  const tools = document.querySelector('.lv-composer-tools');
  if (tools) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.id = 'workspace-chip';
    chip.className = 'lv-model-chip lv-ws-chip';
    chip.setAttribute('aria-haspopup', 'menu');
    chip.setAttribute('aria-expanded', 'false');
    chip.innerHTML =
      '<span class="lv-ws-chip-icon" aria-hidden="true"></span><span class="lv-ws-chip-label lv-model-chip-label"></span>';
    chip.addEventListener('click', (e) => {
      e.stopPropagation();
      openChipMenu();
    });
    const mode = document.getElementById('mode-chip');
    tools.insertBefore(chip, mode && mode.parentElement === tools ? mode : null);
    _chip = chip;
  }

  store.on('session-id', () => {
    renderChip();
    syncScope();
  });
  // Opening a chat makes its directory this tab's pick: a new chat then
  // starts where the person was just working.
  store.on('session-meta', (/** @type {any} */ data) => {
    if (!data?.id) return;
    _rootOf.set(data.id, data.workspace_root ?? null);
    if (data.id === store.sessionId && data.workspace_root && entry(data.workspace_root)) {
      _picked = data.workspace_root;
      try {
        sessionStorage.setItem(PICK_KEY, _picked);
      } catch {
        /* storage off */
      }
    }
    renderChip();
    syncScope();
  });
  store.on('sessions-loaded', () => {
    renderChip();
    syncScope();
  });
  store.on('session-workspace-changed', (/** @type {any} */ data) => {
    if (data?.session_id && 'workspace_root' in data) noteRoot(data.session_id, data.workspace_root);
  });
  store.on('draft-attachments', (/** @type {number} */ n) => {
    _draftHasFiles = n > 0;
    renderChip();
  });
  renderChip();
  syncScope();
  store.emit('workspaces-changed');
}
