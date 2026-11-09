// workspace-mode.js — the composer's permission-mode chip: which mode this
// chat's runs use (read-only / coding / trusted). Present only when the
// server lets chats pick one (/api/info workspace_modes, from
// create_app(workspaces=)).
//
// A new chat carries the picked mode in its first message (workspace_mode);
// an existing chat keeps its own, switched with PATCH /api/sessions/{id} and
// applied from its next run — a reply already streaming finishes as it
// began. A stored mode the server no longer offers runs capped down; the chip
// shows what runs get and says why.
import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { toast } from './toast.js';

/** @type {HTMLButtonElement | null} */
let _chip = null;
/** @type {HTMLElement | null} */
let _menu = null;
/** The mode a new (unsaved) chat starts in unless one is picked: the server's default. */
let _default = /** @type {string | null} */ (null);
/** The mode the new (unsaved) chat on screen will start in. */
let _draft = /** @type {string | null} */ (null);
/** Whether the person picked the draft (a Settings default change then leaves it be). */
let _draftPicked = false;
/** The open chat's stored mode, once known (undefined: not loaded yet). */
let _current = /** @type {string | null | undefined} */ (undefined);
/**
 * When the open chat's mode last came from a source that is current by
 * construction — a change event, this page's own switch, opening the chat.
 * A fetched answer (list snapshot, chat detail) requested before that is
 * stale and dropped: it could only put an older mode back.
 */
let _changedAt = 0;
/** The chat id the chip last showed (null: a new chat). */
let _shownId = /** @type {string | null} */ (null);

const ORDER = ['readonly', 'coding', 'trusted'];

function offer() {
  return store.serverInfo?.workspace_modes || null;
}

/** @param {string | null | undefined} mode */
function label(mode) {
  return mode ? t(`cfg.mode${mode[0].toUpperCase()}${mode.slice(1)}`) : t('mode.default');
}

/**
 * What a chat storing `mode` runs in here: itself when offered, else the
 * widest offered mode below it (the server's rule).
 * @param {string | null | undefined} mode
 */
function effective(mode) {
  const modes = offer()?.modes || [];
  if (!mode || modes.includes(mode)) return mode ?? null;
  const rank = ORDER.indexOf(mode);
  const below = modes.filter((m) => ORDER.indexOf(m) <= rank);
  return below[below.length - 1] ?? null;
}

/** The mode the chip stands for: the open chat's, or the new chat's draft. */
function shown() {
  return store.sessionId ? _current : _draft;
}

/**
 * Show `mode` for the open chat as fetched at `requestedAt`, unless a newer
 * change already landed.
 * @param {string | null | undefined} mode
 * @param {number} requestedAt
 */
function applyFetched(mode, requestedAt) {
  if (requestedAt < _changedAt) return;
  _current = mode ?? null;
  render();
}

/**
 * The mode a new chat's first message asks for, or undefined when the open
 * chat already exists (its own applies). Read by the composer's send.
 */
export function draftWorkspaceMode() {
  return !store.sessionId && offer() ? _draft ?? undefined : undefined;
}

function render() {
  if (!_chip) return;
  const stored = shown();
  const runs = effective(stored);
  _chip.hidden = stored === undefined && !!store.sessionId;
  const text = _chip.querySelector('.lv-model-chip-label');
  if (text) text.textContent = label(runs);
  _chip.title =
    stored && runs !== stored
      ? t('mode.capped', { mode: label(stored), runs: label(runs) })
      : t('mode.chipTitle', { mode: label(runs) });
}

function closeMenu() {
  if (!_menu) return;
  _menu.remove();
  _menu = null;
  _chip?.setAttribute('aria-expanded', 'false');
}

/** @param {string} mode */
async function pick(mode) {
  closeMenu();
  const id = store.sessionId;
  if (!id) {
    _draft = mode;
    _draftPicked = true;
    render();
    return;
  }
  // No "unchanged" shortcut: the shown mode may be stale (another tab), and
  // the server's answer is what counts.
  try {
    await api.setSessionMode(id, mode);
  } catch (err) {
    toast(String(/** @type {Error} */ (err).message || err), { type: 'error' });
    return;
  }
  if (store.sessionId !== id) return;
  _current = mode;
  _changedAt = store.stamp();
  render();
  const live = store.activeRuns.has(id) || store.streaming;
  toast(t(live ? 'mode.nextRun' : 'mode.switched', { mode: label(mode) }));
}

function openMenu() {
  if (_menu) return closeMenu();
  const info = offer();
  if (!info || !_chip) return;
  const menu = document.createElement('div');
  menu.className = 'lv-model-chip-menu lv-mode-menu';
  menu.setAttribute('role', 'menu');
  const title = document.createElement('div');
  title.className = 'lv-model-chip-menu-title';
  title.textContent = t(store.sessionId ? 'mode.menuTitle' : 'mode.newChatTitle');
  menu.appendChild(title);
  const current = effective(shown());
  for (const mode of info.modes) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'lv-model-chip-item lv-mode-item';
    item.setAttribute('role', 'menuitemradio');
    item.setAttribute('aria-checked', String(mode === current));
    item.setAttribute('aria-label', label(mode));
    const text = document.createElement('span');
    text.className = 'lv-mode-item-text';
    const name = document.createElement('span');
    name.className = 'lv-model-chip-item-name';
    name.textContent = label(mode);
    const hint = document.createElement('span');
    hint.className = 'lv-mode-item-hint';
    hint.textContent = t(`cfg.modeHint_${mode}`);
    text.append(name, hint);
    item.appendChild(text);
    if (mode === current) {
      const check = document.createElement('span');
      check.className = 'lv-model-chip-item-check';
      check.textContent = '✓';
      item.appendChild(check);
    }
    item.addEventListener('click', () => pick(mode));
    menu.appendChild(item);
  }
  _chip.insertAdjacentElement('afterend', menu);
  _menu = menu;
  _chip.setAttribute('aria-expanded', 'true');
  /** @type {HTMLElement | null} */ (menu.querySelector('[aria-checked="true"]'))?.focus();
}

/** A new chat starts from the default: a pick was for the chat it started. */
function resetDraft() {
  _draft = _default;
  _draftPicked = false;
}

/**
 * @param {string | null} id
 * @param {boolean} [switched] False only at boot: the page's first fetches
 *   for the chat were requested before the chip existed and still count.
 */
function onSessionId(id, switched = true) {
  closeMenu();
  const wasNew = _shownId === null;
  _shownId = id;
  if (switched) _changedAt = store.stamp(); // a fetch for the chat before is moot
  if (!id) {
    _current = undefined;
    resetDraft();
  } else if (wasNew && store.streaming) {
    // The new chat just got its id from its first run: it was saved with
    // the draft mode that message carried.
    _current = _draft;
  } else {
    // The list knows top-level chats; a task chat's mode arrives with its
    // detail ('session-meta').
    const row = store.sessions.find((s) => s.id === id);
    _current = row ? (row.workspace_mode ?? null) : undefined;
  }
  render();
}

/**
 * Boot the chip. Called from main.js once /api/info resolved; a no-op on a
 * server whose chats can't pick a mode.
 */
export function initWorkspaceMode() {
  const info = offer();
  const inner = document.querySelector('.lv-composer-inner');
  if (!info || !inner || _chip) return;
  _default = _draft = info.default ?? null;
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.id = 'mode-chip';
  chip.className = 'lv-model-chip lv-mode-chip';
  chip.setAttribute('aria-haspopup', 'menu');
  chip.setAttribute('aria-expanded', 'false');
  const text = document.createElement('span');
  text.className = 'lv-model-chip-label';
  chip.appendChild(text);
  chip.addEventListener('click', (e) => {
    e.stopPropagation();
    openMenu();
  });
  const before = document.getElementById('model-chip') || document.getElementById('context-ring');
  inner.insertBefore(chip, before);
  _chip = chip;

  document.addEventListener('click', (e) => {
    if (_menu && !_menu.contains(/** @type {Node} */ (e.target))) closeMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _menu) {
      e.stopPropagation();
      closeMenu();
      _chip?.focus();
    }
  });

  store.on('session-id', (/** @type {string | null} */ id) => onSessionId(id));
  // A list snapshot — the first load, or the one after an event-stream
  // reconnect — is the truth for the open chat's mode. After a (re)connect,
  // a chat the list doesn't show (past the first page, filtered out by a
  // search, a task chat) reads its own detail instead: a switch made in
  // another tab while disconnected sent no event here.
  let resync = false;
  store.on('events-open', () => {
    resync = true; // settled by the snapshot this connect fetches
  });
  store.on('sessions-loaded', async (/** @type {any} */ loaded) => {
    const id = store.sessionId;
    const row = id ? store.sessions.find((s) => s.id === id) : undefined;
    const pending = resync;
    resync = false;
    if (row) {
      applyFetched(row.workspace_mode, loaded?.requestedAt ?? 0);
      return;
    }
    if (!id || !pending) return;
    try {
      const requestedAt = store.stamp();
      const data = await api.getSession(id);
      if (store.sessionId !== id) return; // moved on meanwhile
      applyFetched(data.workspace_mode, requestedAt);
    } catch {
      /* transient — the next switch or reconnect resyncs */
    }
  });
  // "New chat" on a new chat changes no id, but starts over all the same.
  store.on('new-chat', () => {
    if (store.sessionId) return; // the id change resets it
    closeMenu();
    resetDraft();
    render();
  });
  // The open chat's detail (a switch, a refresh) carries its stored mode.
  store.on('session-meta', (/** @type {any} */ data) => {
    if (data?.id !== store.sessionId) return;
    applyFetched(data.workspace_mode, data.requestedAt ?? 0);
  });
  // Another tab (or this one) switched a chat's mode.
  store.on('session-workspace-changed', (/** @type {any} */ data) => {
    const row = store.sessions.find((s) => s.id === data?.session_id);
    if (row) row.workspace_mode = data.workspace_mode;
    if (data?.session_id === store.sessionId) {
      _current = data.workspace_mode ?? null;
      _changedAt = store.stamp();
      render();
    }
  });
  // A Settings change of the default reaches a draft nobody picked yet.
  store.on('config-loaded', (/** @type {any} */ cfg) => {
    const mode = cfg?.workspace?.effective_mode;
    if (!mode) return;
    _default = effective(mode);
    if (_draftPicked) return;
    _draft = _default;
    render();
  });
  onSessionId(store.sessionId, false);
}
