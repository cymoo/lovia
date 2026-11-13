// The composer's `/` menu: typing `/` at the very start of a message offers
// the chat's skills, filtered as you type; picking one puts `/name ` there.
// The text is sent as typed — the Skills prompt tells the model what a
// leading `/name` means, so the server expands nothing.
import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { draftWorkspaceRoot } from './workspaces.js';
import { initCompleter } from './composer-complete.js';

/** @typedef {{ name: string, description: string }} SkillInfo */

// What the Skills prompt rule recognizes: a skill name (letters, digits,
// `-`/`_` between them) right after a `/` that opens the message.
const SLASH = /^\/([A-Za-z0-9_-]*)$/;

/**
 * The name being typed when the caret sits in a `/name` that opens the
 * message, else null. Only from the very first character — the rule the
 * model follows ignores a `/name` anywhere else.
 * @param {string} text
 * @param {number} caret
 * @returns {string | null}
 */
export function slashQuery(text, caret) {
  const m = SLASH.exec(text.slice(0, caret));
  return m ? m[1] : null;
}

/**
 * The skills matching `query`, case-insensitively: names that start with it
 * first, then those that only contain it — each group in catalog order.
 * @param {SkillInfo[]} skills
 * @param {string} query
 * @returns {SkillInfo[]}
 */
export function matchSkills(skills, query) {
  const q = query.toLowerCase();
  const starts = [];
  const contains = [];
  for (const s of skills) {
    const name = s.name.toLowerCase();
    if (name.startsWith(q)) starts.push(s);
    else if (name.includes(q)) contains.push(s);
  }
  return [...starts, ...contains];
}

/**
 * `text` with the `/name` token that opens it replaced by `/<name> `, and
 * where the caret goes: after that space.
 * @param {string} text
 * @param {string} name
 * @returns {{ text: string, caret: number }}
 */
export function insertSkill(text, name) {
  const end = /^\/[A-Za-z0-9_-]*/.exec(text)?.[0].length ?? 0;
  const rest = text.slice(end).replace(/^[ \t]/, '');
  const head = `/${name} `;
  return { text: head + rest, caret: head.length };
}

/**
 * Wire the menu to the composer's textarea. Each opening fetches the list
 * afresh: a skill added on disk, another chat's or agent's list.
 * @param {HTMLTextAreaElement | null} input
 */
export function initSkillMenu(input) {
  if (!input) return;
  /** @type {import('./composer-complete.js').CompleterSpec<SkillInfo>} */
  const spec = {
    id: 'lv-skill-menu',
    className: 'lv-skill-menu',
    label: () => t('composer.skills'),
    query: slashQuery,
    async load() {
      const where = store.sessionId
        ? { session: store.sessionId }
        : { agent: store.agent || undefined, root: draftWorkspaceRoot() || undefined };
      return { items: await api.listSkills(where) };
    },
    match: matchSkills,
    row(s) {
      const name = document.createElement('span');
      name.className = 'lv-popup-menu-label';
      name.textContent = `/${s.name}`;
      const desc = document.createElement('span');
      desc.className = 'lv-complete-menu-desc';
      desc.textContent = s.description;
      return [name, desc];
    },
    insert: (text, _caret, s) => insertSkill(text, s.name),
  };
  initCompleter(input, spec);
}
