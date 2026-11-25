// What runs (or ran) behind a chat, as one list: its subagent tasks (chats of
// their own) and its background commands. The two keep their own data models;
// this is where they meet. Pure: background.js draws the result.

/**
 * @typedef {object} BackgroundItem
 * @property {string} key `task:<session>` or `cmd:<process>`.
 * @property {'task' | 'cmd'} kind
 * @property {string} id
 * @property {string} tag A task's `t1`; '' for a command.
 * @property {string} title
 * @property {'approval' | 'running' | 'completed' | 'failed' | 'stopped' | 'interrupted' | 'lost'} state
 * @property {boolean} live
 * @property {boolean} failed
 * @property {number} startedAt Unix seconds; 0 when not known.
 * @property {number | null} endedAt
 * @property {number | null} exitCode
 * @property {string | null} endedBy
 * @property {string} line A running command's last line of output.
 */

/** A task's lifecycle: its live run wins, else how its last one ended. */
export function taskState(task, run) {
  if (run) return run.pending_approval || run.waits_on ? 'approval' : 'running';
  switch (task.last_run_status) {
    case 'failed':
      return 'failed';
    case 'cancelled':
      return 'stopped';
    case 'interrupted':
      return 'interrupted';
    default:
      return 'completed'; // no record of a run: nothing to say went wrong
  }
}

/** "[t1] dig into …" → its tag and its name. */
export function taskLabel(task) {
  const m = /^\[([^\]]{1,8})\]\s*(.*)$/s.exec(task.title || '');
  return { tag: m ? m[1] : '', title: (m ? m[2] : task.title) || task.id };
}

/** @returns {BackgroundItem} */
function taskItem(task, run) {
  const state = taskState(task, run);
  return {
    key: `task:${task.id}`,
    kind: 'task',
    id: task.id,
    ...taskLabel(task),
    state,
    live: !!run,
    failed: state === 'failed',
    startedAt: run?.started_at || task.created_at || 0,
    endedAt: null,
    exitCode: null,
    endedBy: null,
    line: '',
  };
}

/** @returns {BackgroundItem} */
function commandItem(p) {
  const live = p.status === 'running';
  // Stopped is not failed: someone meant it, or nothing in it went wrong.
  const failed = p.status === 'exited' && p.exit_code !== 0;
  const state = live
    ? 'running'
    : p.ended_by === 'lost'
      ? 'lost'
      : p.status === 'killed'
        ? 'stopped'
        : failed
          ? 'failed'
          : 'completed';
  return {
    key: `cmd:${p.process_id}`,
    kind: 'cmd',
    id: p.process_id,
    tag: '',
    title: p.command.split(/\s+/).join(' ').trim(),
    state,
    live,
    failed,
    startedAt: p.started_at || 0,
    endedAt: p.ended_at ?? null,
    exitCode: p.exit_code ?? null,
    endedBy: p.ended_by ?? null,
    line: live ? p.last_line || '' : '',
  };
}

/**
 * @param {any[]} procs GET …/processes
 * @param {any[]} tasks The chat's task sessions.
 * @param {Map<string, any> | undefined} runs Live and waiting runs, by session.
 * @returns {{ live: BackgroundItem[], ended: BackgroundItem[] }} What waits
 *   on the user first, then newest first.
 */
export function buildItems(procs, tasks, runs) {
  const items = [
    ...tasks.map((task) => taskItem(task, runs?.get(task.id))),
    ...procs.map(commandItem),
  ];
  const waits = (item) => (item.state === 'approval' ? 0 : 1);
  items.sort((a, b) => waits(a) - waits(b) || b.startedAt - a.startedAt);
  return { live: items.filter((i) => i.live), ended: items.filter((i) => !i.live) };
}

/**
 * The topbar button, by the first that holds: something waits on the user,
 * a failure nobody has seen, a task at work, only commands at work, all over.
 * @param {{ live: BackgroundItem[], ended: BackgroundItem[] }} items
 * @param {Set<string>} unseen Keys of failures not seen yet.
 * @returns {{ tone: 'approval' | 'failed' | 'running' | 'steady' | 'idle', running: number, total: number }}
 */
export function buttonState({ live, ended }, unseen) {
  const tone = live.some((i) => i.state === 'approval')
    ? 'approval'
    : ended.some((i) => unseen.has(i.key))
      ? 'failed'
      : live.some((i) => i.kind === 'task')
        ? 'running'
        : live.length
          ? 'steady'
          : 'idle';
  return { tone, running: live.length, total: live.length + ended.length };
}

/**
 * Failures that happened while the page looked on: what it saw running, or
 * what started since it opened the chat. Not what was over by then — however
 * late the two lists (tasks, commands) come in.
 * @param {() => number} [now] Unix seconds.
 */
export function createFailureWatch(now = () => Date.now() / 1000) {
  let chat = /** @type {string | null} */ (null);
  let opened = 0;
  const running = new Set(); // seen live in this chat
  const told = new Set();
  const unseen = new Set();
  return {
    unseen,
    /**
     * @param {string | null} sessionId
     * @param {{ live: BackgroundItem[], ended: BackgroundItem[] }} items
     */
    take(sessionId, { live, ended }) {
      if (sessionId !== chat) {
        chat = sessionId;
        opened = now();
        for (const keys of [running, told, unseen]) keys.clear();
      }
      for (const i of live) {
        running.add(i.key);
        told.delete(i.key); // a task may run, and fail, again
      }
      for (const i of ended) {
        if (!i.failed || told.has(i.key)) continue;
        told.add(i.key);
        if (running.has(i.key) || i.startedAt >= opened) unseen.add(i.key);
      }
      for (const key of unseen) if (!ended.some((i) => i.key === key)) unseen.delete(key);
    },
    seen() {
      unseen.clear();
    },
  };
}
