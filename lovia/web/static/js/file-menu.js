// The composer's `@` menu: typing `@` at the start of the message or after
// whitespace offers the workspace's files, matched as you type; picking one
// puts `@path ` there (`@"path" ` when it has whitespace). Only the path is
// sent, as typed — the model reads the file itself with read_file.
import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { draftWorkspaceRoot } from './workspaces.js';
import { initCompleter } from './composer-complete.js';

// The token being typed: `@` opening the message or after whitespace, then
// either a quoted path (`@"my fi`, the quote not closed yet) or a bare one.
const AT = /(?:^|\s)@(?:"([^"\n]*)|([^\s"]*))$/;

// Rows the menu shows at most; the rest are a keystroke away.
const SHOWN = 50;

/**
 * The path being typed when the caret sits in an `@` token, else null.
 * @param {string} text
 * @param {number} caret
 * @returns {string | null}
 */
export function atQuery(text, caret) {
  const m = AT.exec(text.slice(0, caret));
  return m ? m[1] ?? m[2] : null;
}

/**
 * Whether `@path` can say `path` unambiguously. A double quote would end
 * the quoted form early, and a control character or line break would break
 * the message's text, so such paths are left out of the menu.
 * @param {string} path
 */
export function mentionable(path) {
  return !/["\p{Cc}\p{Zl}\p{Zp}]/u.test(path);
}

/**
 * The paths matching `query`, case-insensitively, at most `limit`: file
 * names starting with it, then names containing it, then paths containing
 * it, then paths holding its characters in order. Shorter paths first in
 * each group, then by path.
 * @param {string[]} paths
 * @param {string} query
 * @param {number} [limit]
 * @returns {string[]}
 */
export function matchFiles(paths, query, limit = SHOWN) {
  const q = query.toLowerCase();
  /** @type {string[][]} */
  const tiers = [[], [], [], []];
  for (const path of paths) {
    const p = path.toLowerCase();
    const name = p.slice(p.lastIndexOf('/') + 1);
    if (name.startsWith(q)) tiers[0].push(path);
    else if (name.includes(q)) tiers[1].push(path);
    else if (p.includes(q)) tiers[2].push(path);
    else if (inOrder(p, q)) tiers[3].push(path);
  }
  const out = [];
  for (const tier of tiers) {
    tier.sort((a, b) => a.length - b.length || (a < b ? -1 : a > b ? 1 : 0));
    out.push(...tier.slice(0, limit - out.length));
    if (out.length >= limit) break;
  }
  return out;
}

/** @param {string} s @param {string} q */
function inOrder(s, q) {
  let i = 0;
  for (const ch of s) if (ch === q[i] && ++i === q.length) return true;
  return false;
}

/**
 * `text` with the `@` token at `caret` replaced by `@path ` (quoted when the
 * path has whitespace), and where the caret goes: after that space. The
 * token's rest past the caret goes too, as does one space after it.
 * @param {string} text
 * @param {number} caret
 * @param {string} path
 * @returns {{ text: string, caret: number }}
 */
export function insertFile(text, caret, path) {
  const m = AT.exec(text.slice(0, caret));
  let start = caret;
  let tail = text.slice(caret);
  if (m) {
    start = caret - m[0].length + m[0].indexOf('@');
    const rest = m[1] !== undefined ? /^[^"\n]*"?/ : /^\S*/; // both always match
    tail = tail.slice(/** @type {RegExpExecArray} */ (rest.exec(tail))[0].length);
  }
  const token = /\s/.test(path) ? `@"${path}"` : `@${path}`;
  const head = `${text.slice(0, start)}${token} `;
  return { text: head + tail.replace(/^[ \t]/, ''), caret: head.length };
}

// The list is kept per chat (one at a time) and dropped at the end of every
// run — the agent may have added files, through its tools or the shell —
// and when the folder the workspace routes serve changes under the same
// key: an old chat linked to a folder keeps its session id.
/** @type {{ key: string, list: { items: string[], note: string | null } } | null} */
let cached = null;
let generation = 0;
for (const event of ['run-end', 'workspace-scope-changed']) {
  store.on(event, () => {
    cached = null;
    generation++;
  });
}

function hasWorkspace() {
  return !!store.agents.find((a) => a.name === store.agent)?.workspace;
}

async function loadPaths() {
  if (!hasWorkspace()) return null;
  const where = store.sessionId
    ? { session: store.sessionId }
    : { agent: store.agent || undefined, root: draftWorkspaceRoot() || undefined };
  const key = JSON.stringify(where);
  if (cached?.key === key) return cached.list;
  const ticket = generation;
  const got = await api.workspacePaths(where);
  const items = got.paths.filter(mentionable);
  const list = {
    items,
    note: got.truncated ? t('composer.filesTruncated', { n: items.length }) : null,
  };
  // A run ended while this was out: it may be missing that run's files.
  if (ticket === generation) cached = { key, list };
  return list;
}

/** @param {HTMLTextAreaElement | null} input */
export function initFileMenu(input) {
  if (!input) return;
  /** @type {import('./composer-complete.js').CompleterSpec<string>} */
  const spec = {
    id: 'lv-file-menu',
    className: 'lv-file-menu',
    label: () => t('composer.files'),
    query: atQuery,
    load: loadPaths,
    match: (items, query) => matchFiles(items, query),
    row(path) {
      const cut = path.lastIndexOf('/');
      const name = document.createElement('span');
      name.className = 'lv-popup-menu-label';
      name.textContent = path.slice(cut + 1);
      const dir = document.createElement('span');
      dir.className = 'lv-complete-menu-desc';
      dir.textContent = cut > 0 ? path.slice(0, cut) : '';
      return [name, dir];
    },
    insert: insertFile,
  };
  initCompleter(input, spec);
}
