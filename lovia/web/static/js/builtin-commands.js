// builtin-commands.js — the slash commands the page comes with. A command
// does something, or takes what is typed after it; what a control beside
// the composer already does in one click (the model, the mode) gets none.
// `/init` sends a message — a prompt of ours, to write the project's
// AGENTS.md.
import { store } from './store.js';
import { t } from './i18n.js';
import { registerCommand } from './commands.js';
import { sendText } from './chat.js';
import { openChatMcp } from './project-mcp.js';
import { forkChat, openChat, renameSession } from './sessions.js';

// For the model: plain instructions, English whatever the page's language
// (the model answers in the user's).
const INIT_PROMPT = `Read through this project, then write an AGENTS.md at its root — or update the one that is there — for an AI coding agent that starts with no context.

Cover only what it can't guess: what the project is; how to set up, build, run, test and lint it (the exact commands); how the code is laid out; conventions that differ from the usual; things that will bite. Verify commands against the project's own files rather than assuming them.

Keep it short and specific to this project — no generic advice. If an AGENTS.md exists, keep what is still true and fix what isn't.`;

/** Whether the composer's agent has a workspace to write the file in. */
function hasWorkspace() {
  return !!store.agents.find((a) => a.name === store.agent)?.workspace;
}

/** An open chat that is listed (a task chat has no branch). */
function listedChat() {
  const s = openChat();
  return s && !s.parent_id ? s : null;
}

export function initBuiltinCommands() {
  registerCommand('mcp', {
    description: t('command.mcp'),
    when: () => store.canConfigure && store.configured,
    run: () => openChatMcp(),
  });
  registerCommand('init', {
    description: t('command.init'),
    when: hasWorkspace,
    run: () => sendText(INIT_PROMPT),
  });
  registerCommand('rename', {
    description: t('command.rename'),
    argument: t('command.renameArg'),
    when: () => !!openChat(),
    // No title typed: the dialog asks, as the menu's Rename does.
    run: ({ args }) => renameSession(openChat(), args || undefined),
  });
  registerCommand('branch', {
    description: t('command.branch'),
    when: () => !!listedChat(),
    run: () => void forkChat(listedChat().id),
  });
}
