// A background task's report, as it lands in the chat that spawned it: a user
// message the core Subagents plugin writes (`_render` in
// lovia/plugins/subagents.py) — `[subagent t1: done] agent=NAME elapsed=12s`,
// then the body. The chat draws it as a card, not as something the user said.
// Word for word with `_render`: tests/web/test_subagents.py pins the format.

const REPORT = /^\[subagent ([^\s:\]]+): (done|failed)\] agent=(.*?) elapsed=(\d+)s(?:\n([\s\S]*))?$/;

/**
 * @typedef {{ id: string, ok: boolean, agent: string, elapsed: number, body: string }} SubagentReport
 */

/**
 * The report `text` is, or null for any other message.
 * @param {string} text
 * @returns {SubagentReport | null}
 */
export function parseReport(text) {
  const m = REPORT.exec(text);
  if (!m) return null;
  return { id: m[1], ok: m[2] === 'done', agent: m[3], elapsed: Number(m[4]), body: m[5] ?? '' };
}
