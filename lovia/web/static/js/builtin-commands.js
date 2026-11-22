// builtin-commands.js — the slash commands the page comes with. A command
// does something, or takes what is typed after it; what a control beside
// the composer already does in one click (the model, the mode) gets none.
// `/review` sends a code-review prompt to the assistant.
import { store } from './store.js';
import { t } from './i18n.js';
import { registerCommand } from './commands.js';
import { sendText } from './chat.js';
import { forkChat, openChat } from './sessions.js';

const REVIEW_PROMPT = `Review the current workspace's uncommitted code changes. Inspect Git status, staged and unstaged diffs, and relevant untracked source files. Read surrounding code as needed to verify each finding. If there are no changes or you cannot inspect them, say so and stop.

Report only concrete, actionable problems introduced by the changes. Skip style nits, speculation, and unrelated pre-existing issues. List findings from most to least severe. For each, give its priority, the changed file and line, and a brief explanation of when it causes a problem and why it matters.

Finish with a brief overall assessment. If there are no actionable findings, say so. This is a read-only review: do not modify files or apply fixes.`;

/** Whether the composer's agent has a workspace to review. */
function hasWorkspace() {
  return !!store.agents.find((a) => a.name === store.agent)?.workspace;
}

/** An open chat that is listed (a task chat has no branch). */
function listedChat() {
  const s = openChat();
  return s && !s.parent_id ? s : null;
}

export function initBuiltinCommands() {
  registerCommand('review', {
    description: t('command.review'),
    when: hasWorkspace,
    run: () => sendText(REVIEW_PROMPT),
  });
  registerCommand('branch', {
    description: t('command.branch'),
    when: () => !!listedChat(),
    run: () => void forkChat(listedChat().id),
  });
}
