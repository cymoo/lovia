// commands.js — the composer's slash commands (lovia.registerCommand).
//
// A message that starts with `/name` and names a command runs it here and
// is never sent: `/rename New title`, `/mcp`. The `/` menu lists commands
// ahead of the chat's skills (skill-menu.js); a `/name` that is no command
// goes to the model as typed, where it may name a skill. A command and a
// skill of one name: the command wins.
//
// The registry and the lookup live here; the built-in commands are in
// builtin-commands.js, a deployer's in its extension script.

import { Registry } from './registry.js';
import { toast } from './toast.js';

const FIELDS = new Set(['description', 'argument', 'when', 'run']);
// What a `/name` may be — the skill menu's own token, lower-case.
const NAME = /^[a-z][a-z0-9-]*$/;
const TYPED = /^\/([a-z][a-z0-9-]*)(?:\s+([\s\S]*))?$/;

/**
 * @typedef {object} Command
 * @property {string} description One line, shown beside the name in the menu.
 * @property {string} [argument] What goes after the name (a placeholder, as
 *   in "/rename New title"). With one, picking the command from the menu
 *   leaves `/name ` to finish and send; without, it runs at once — and a
 *   message with more after the name is not the command.
 * @property {() => boolean} [when] Whether it is offered now; always when absent.
 * @property {(ctx: { args: string, sessionId: string | null }) => void | Promise<void>} run
 *   `args` is the text after the name, trimmed. An error it throws shows as a toast.
 */

/** @type {Registry<Command>} */
const commands = new Registry();

/**
 * Add a slash command, or replace the one named `name`.
 * @param {string} name Lower-case letters, digits and `-`; typed as `/name`.
 * @param {Command} command
 * @returns {() => void} Removes it, bringing back the command it replaced.
 */
export function registerCommand(name, command) {
  if (typeof name !== 'string' || !NAME.test(name)) {
    throw new TypeError(
      'lovia.registerCommand: a name is lower-case letters, digits and "-", starting with a letter',
    );
  }
  if (!command || typeof command !== 'object') {
    throw new TypeError(`lovia.registerCommand(${JSON.stringify(name)}): expected a command`);
  }
  for (const key of Object.keys(command)) {
    if (!FIELDS.has(key)) {
      throw new TypeError(`lovia.registerCommand: unknown field ${JSON.stringify(key)}`);
    }
  }
  if (typeof command.description !== 'string' || !command.description) {
    throw new TypeError('lovia.registerCommand: `description` must be a non-empty string');
  }
  if (command.argument !== undefined && typeof command.argument !== 'string') {
    throw new TypeError('lovia.registerCommand: `argument` must be a string');
  }
  if (command.when !== undefined && typeof command.when !== 'function') {
    throw new TypeError('lovia.registerCommand: `when` must be a function');
  }
  if (typeof command.run !== 'function') {
    throw new TypeError('lovia.registerCommand: `run` is required');
  }
  return commands.add(name, Object.freeze({ ...command }));
}

/** @param {string} name @param {Command} command */
function offered(name, command) {
  if (!command.when) return true;
  try {
    return !!command.when();
  } catch (err) {
    console.error(`lovia: command "/${name}" failed:`, err);
    return false;
  }
}

/**
 * The commands on offer now, in the order they were registered.
 * @returns {{ name: string, description: string, argument?: string }[]}
 */
export function listCommands() {
  const out = [];
  for (const [name, command] of commands.entries()) {
    if (offered(name, command)) {
      out.push({ name, description: command.description, argument: command.argument });
    }
  }
  return out;
}

/**
 * The command a message names and its argument text, or null: not a
 * `/name …`, no such command, one that isn't offered now — or text after
 * a command that takes none ("/model is broken, look at…" is a message).
 * @param {string} text The whole message, trimmed.
 * @returns {{ name: string, args: string } | null}
 */
export function findCommand(text) {
  const typed = TYPED.exec(text);
  if (!typed) return null;
  const command = commands.get(typed[1]);
  if (!command || !offered(typed[1], command)) return null;
  const args = (typed[2] ?? '').trim();
  if (args && command.argument === undefined) return null;
  return { name: typed[1], args };
}

/**
 * Run the command named `name`. A failure is a toast, never an exception.
 * @param {string} name
 * @param {string} args
 * @param {string | null} sessionId
 */
export async function runCommand(name, args, sessionId) {
  const command = commands.get(name);
  if (!command) return;
  try {
    await command.run(Object.freeze({ args, sessionId }));
  } catch (err) {
    console.error(`lovia: command "/${name}" failed:`, err);
    toast(String(/** @type {any} */ (err)?.message || err), { type: 'error' });
  }
}
