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
//
// The open chat's mode reaches the chip from several sources — a change
// event, this page's own switch, the session list, the chat's detail — in no
// guaranteed order. Each carries the chat's workspace_mode_rev, which the
// server bumps on every switch: the chip takes a report only when its
// revision is higher than the one it shows. That one rule orders them all.
import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { icon } from './icons.js';

/** @type {HTMLButtonElement | null} */
let _chip = null;
/** @type {HTMLElement | null} */
let _menu = null;
/** The mode the new (unsaved) chat on screen will start in. */
let _draft = /** @type {string | null} */ (null);
/** Whether the person picked the draft (a Settings change then leaves it be). */
let _draftPicked = false;
/** The open chat's stored mode, once known (undefined: not loaded yet). */
let _current = /** @type {string | null | undefined} */ (undefined);
/** The revision of `_current` (-1: none seen for the open chat yet). */
let _rev = -1;
/** The chat id the chip last showed (null: a new chat). */
let _shownId = /** @type {string | null} */ (null);

const ORDER = ['readonly', 'coding', 'trusted'];
/** Each mode's glyph: view, edit, everything. */
const GLYPH = /** @type {Record<string, string>} */ ({
  readonly: 'shield-check',
  coding: 'shield-pen',
  trusted: 'shield-alert',
});

function offer() {
  return store.serverInfo?.workspace_modes || null;
}

/** The mode the composer's agent starts chats in, where the server says. */
function agentMode() {
  return store.agents.find((/** @type {any} */ a) => a.name === store.agent)?.mode ?? null;
}

/**
 * The mode a chat with none stored runs in — its agent's own workspace —
 * where the server names it (the CLI's), else null.
 */
function own() {
  return agentMode() || offer()?.own || null;
}

/** What a new chat starts in unless one is picked: its agent's mode, else the server's. */
function defaultMode() {
  return effective(agentMode() || offer()?.default);
}

/**
 * A mode's name. None stored means the agent's own workspace: named by the
 * mode it runs in where the server says, else "Default".
 * @param {string | null | undefined} mode
 */
function label(mode) {
  const named = mode || own();
  return named ? t(`cfg.mode${named[0].toUpperCase()}${named.slice(1)}`) : t('mode.default');
}

/** @param {string | null | undefined} mode */
function glyph(mode) {
  return icon(GLYPH[mode || own() || ''] || 'shield-check', { size: 14 });
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
 * Take a report of the open chat's mode — any source — if it is newer than
 * what the chip shows.
 * @param {{ workspace_mode?: string | null, workspace_mode_rev?: number } | null | undefined} report
 */
function apply(report) {
  const rev = report?.workspace_mode_rev ?? 0;
  if (!report || rev <= _rev) return;
  _rev = rev;
  _current = report.workspace_mode ?? null;
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
  const mark = _chip.querySelector('.lv-mode-chip-icon');
  if (mark) mark.innerHTML = glyph(runs);
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
  let answer;
  try {
    answer = await api.setSessionMode(id, mode);
  } catch (err) {
    toast(String(/** @type {Error} */ (err).message || err), { type: 'error' });
    return;
  }
  if (store.sessionId !== id) return;
  apply(answer); // a later switch from another tab, if seen already, wins
  const live = store.activeRuns.has(id) || store.streaming;
  toast(t(live ? 'mode.nextRun' : 'mode.switched', { mode: label(mode) }));
}

function openMenu() {
  if (_menu) return closeMenu();
  const info = offer();
  if (!info || !_chip) return;
  // A new chat's first message is out and its chat not created yet: it was
  // sent with the draft's mode, so a pick now would be silently lost.
  if (!store.sessionId && store.streaming) return;
  const menu = document.createElement('div');
  menu.className = 'lv-model-chip-menu lv-mode-menu';
  menu.setAttribute('role', 'menu');
  const title = document.createElement('div');
  title.className = 'lv-model-chip-menu-title';
  title.textContent = t(store.sessionId ? 'mode.menuTitle' : 'mode.newChatTitle');
  menu.appendChild(title);
  // No stored mode runs in the agent's own, which the chip names too.
  const current = effective(shown()) || own();
  for (const mode of info.modes) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'lv-model-chip-item lv-mode-item';
    item.setAttribute('role', 'menuitemradio');
    item.setAttribute('aria-checked', String(mode === current));
    item.setAttribute('aria-label', label(mode));
    const mark = document.createElement('span');
    mark.className = 'lv-mode-item-icon';
    mark.setAttribute('aria-hidden', 'true');
    mark.innerHTML = glyph(mode);
    const text = document.createElement('span');
    text.className = 'lv-mode-item-text';
    const name = document.createElement('span');
    name.className = 'lv-model-chip-item-name';
    name.textContent = label(mode);
    const hint = document.createElement('span');
    hint.className = 'lv-mode-item-hint';
    hint.textContent = t(`cfg.modeHint_${mode}`);
    text.append(name, hint);
    item.append(mark, text);
    if (mode === current) item.insertAdjacentHTML('beforeend', icon('check', { size: 15 }));
    item.addEventListener('click', () => pick(mode));
    menu.appendChild(item);
  }
  _chip.insertAdjacentElement('afterend', menu);
  // Opens over its chip, wherever the bar put it — pulled back in where the
  // bar is narrower than the menu reaches (a phone).
  const room = (menu.offsetParent?.clientWidth ?? Infinity) - menu.offsetWidth;
  menu.style.left = `${Math.max(0, Math.min(_chip.offsetLeft, room))}px`;
  menu.style.right = 'auto';
  _menu = menu;
  _chip.setAttribute('aria-expanded', 'true');
  /** @type {HTMLElement | null} */ (menu.querySelector('[aria-checked="true"]'))?.focus();
}

/** A new chat starts from the default: a pick was for the chat it started. */
function resetDraft() {
  _draft = defaultMode();
  _draftPicked = false;
}

/** @param {string | null} id */
function onSessionId(id) {
  closeMenu();
  const wasNew = _shownId === null;
  _shownId = id;
  _current = undefined; // a revision belongs to one chat: start over
  _rev = -1;
  if (!id) {
    resetDraft();
  } else if (wasNew && store.streaming) {
    // The new chat just got its id from its first run: it was saved with
    // the draft mode that message carried (revision 0, as reports will say).
    _current = _draft;
  } else {
    // The list knows top-level chats; a task chat's mode arrives with its
    // detail ('session-meta').
    apply(store.sessions.find((s) => s.id === id));
  }
  render();
}

/**
 * A Settings change of an agent's mode (the agent list was refetched). It
 * reaches a new chat's draft nobody picked yet — and the mode named for
 * every chat with none of its own: the agent they run on was rebuilt in it.
 */
function onAgents() {
  if (!_draftPicked) _draft = defaultMode();
  render();
}

/**
 * Boot the chip. Called from main.js once /api/info resolved; a no-op on a
 * server whose chats can't pick a mode.
 */
export function initWorkspaceMode() {
  const info = offer();
  // The composer bar's left side, with what else shapes the next message.
  const tools = document.querySelector('.lv-composer-tools');
  if (!info || !tools || _chip) return;
  _draft = defaultMode();
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.id = 'mode-chip';
  chip.className = 'lv-model-chip lv-mode-chip';
  chip.setAttribute('aria-haspopup', 'menu');
  chip.setAttribute('aria-expanded', 'false');
  const mark = document.createElement('span');
  mark.className = 'lv-mode-chip-icon';
  mark.setAttribute('aria-hidden', 'true');
  const text = document.createElement('span');
  text.className = 'lv-model-chip-label';
  chip.append(mark, text);
  chip.addEventListener('click', (e) => {
    e.stopPropagation();
    openMenu();
  });
  tools.appendChild(chip);
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
  store.on('sessions-loaded', async () => {
    const id = store.sessionId;
    const row = id ? store.sessions.find((s) => s.id === id) : undefined;
    const pending = resync;
    resync = false;
    if (row) {
      apply(row);
      return;
    }
    if (!id || !pending) return;
    try {
      const data = await api.getSession(id);
      if (store.sessionId === id) apply(data); // unless we moved on meanwhile
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
    if (data?.id === store.sessionId) apply(data);
  });
  // Another tab (or this one) switched a chat's mode.
  store.on('session-workspace-changed', (/** @type {any} */ data) => {
    const row = store.sessions.find((s) => s.id === data?.session_id);
    if (row && (data.workspace_mode_rev ?? 0) > (row.workspace_mode_rev ?? 0)) {
      row.workspace_mode = data.workspace_mode;
      row.workspace_mode_rev = data.workspace_mode_rev;
    }
    if (data?.session_id === store.sessionId) apply(data);
  });
  store.on('agents-loaded', onAgents);
  // A new chat's agent decides the mode it starts in; an open chat's names
  // the mode it runs in when it stores none.
  store.on('agent-changed', () => {
    if (!store.sessionId) resetDraft();
    render();
  });
  onSessionId(store.sessionId);
}
