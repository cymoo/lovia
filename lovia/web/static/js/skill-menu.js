// The composer's `/` menu: typing `/` at the very start of a message offers
// the page's commands (commands.js), then the chat's skills, filtered as you
// type. Picking a skill puts `/name ` there, and the text is sent as typed —
// the Skills prompt tells the model what a leading `/name` means, so the
// server expands nothing. Picking a command runs it, or leaves `/name ` for
// the argument it takes; a command is never sent.
import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { draftWorkspaceRoot } from './workspaces.js';
import { initCompleter } from './composer-complete.js';
import { listCommands, runCommand } from './commands.js';

/** @typedef {{ name: string, description: string }} SkillInfo */
/** A row of the menu: a skill, or a command (`argument`: what it takes). @typedef {SkillInfo & { command?: boolean, argument?: string }} SlashItem */

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
 * The menu's rows: the commands on offer, then the skills — less any skill a
 * command's name hides (a `/name` that is a command never reaches the model).
 * @param {{ name: string, description: string, argument?: string }[]} commands
 * @param {SkillInfo[]} skills
 * @returns {SlashItem[]}
 */
export function slashItems(commands, skills) {
  const taken = new Set(commands.map((c) => c.name));
  return [
    ...commands.map((c) => ({ ...c, command: true })),
    ...skills.filter((s) => !taken.has(s.name)),
  ];
}

/**
 * Wire the menu to the composer's textarea. Each opening fetches the list
 * afresh: a skill added on disk, another chat's or agent's list.
 * @param {HTMLTextAreaElement | null} input
 */
export function initSkillMenu(input) {
  if (!input) return;
  /** @type {import('./composer-complete.js').CompleterSpec<SlashItem>} */
  const spec = {
    id: 'lv-skill-menu',
    className: 'lv-skill-menu',
    label: () => t('composer.slash'),
    query: slashQuery,
    async load() {
      const where = store.sessionId
        ? { session: store.sessionId }
        : { agent: store.agent || undefined, root: draftWorkspaceRoot() || undefined };
      // The commands need no server: they are offered even when the skills
      // can't be listed.
      const skills = await api.listSkills(where).catch(() => []);
      return { items: slashItems(listCommands(), skills) };
    },
    // Each group keeps its place: commands first, whatever matched best.
    match: (items, query) => [
      ...matchSkills(items.filter((i) => i.command), query),
      ...matchSkills(items.filter((i) => !i.command), query),
    ],
    group: (item) => t(item.command ? 'composer.commands' : 'composer.skills'),
    row(item) {
      const name = document.createElement('span');
      name.className = 'lv-popup-menu-label';
      name.textContent = `/${item.name}`;
      if (item.argument) {
        const arg = document.createElement('span');
        arg.className = 'lv-complete-menu-arg';
        arg.textContent = ` ${item.argument}`;
        name.append(arg);
      }
      const desc = document.createElement('span');
      desc.className = 'lv-complete-menu-desc';
      desc.textContent = item.description;
      return [name, desc];
    },
    // A command that takes nothing runs on the pick (below), leaving nothing.
    insert: (text, _caret, item) =>
      item.command && !item.argument ? { text: '', caret: 0 } : insertSkill(text, item.name),
    picked(item) {
      if (item.command && !item.argument) void runCommand(item.name, '', store.sessionId);
    },
  };
  initCompleter(input, spec);
}
