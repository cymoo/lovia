// Presentation-only batching shared by catch-up history and live completions.
// Run IDs deduplicate the two paths, including a history response racing an SSE.

/** @typedef {{ id?: string, session_id?: string, status: string }} RunNotice */

/**
 * @param {{ notify: Function, t: Function, titleOf: (sid: string) => string }} deps
 */
export function createRunNotices({ notify, t, titleOf }) {
  const seen = new Set();
  /** @type {RunNotice[]} */
  let pending = [];
  let timer = null;

  /** A resumed run may fail, then complete under the same ID. */
  function remember(/** @type {RunNotice} */ run) {
    if (!run.id) return true; // older event payloads may lack a stable ID
    const key = `${run.id}:${run.status}`;
    if (seen.has(key)) return false;
    seen.add(key);
    if (seen.size > 256) seen.delete(seen.values().next().value);
    return true;
  }

  function show(/** @type {RunNotice[]} */ runs, missed) {
    const failed = runs.filter((r) => r.status === 'failed').length;
    const completed = runs.length - failed;
    const type = failed ? 'error' : 'success';
    let message;
    if (runs.length === 1) {
      const key = missed
        ? (failed ? 'toast.missedRunFailed' : 'toast.missedRunFinished')
        : (failed ? 'toast.runFailed' : 'toast.runFinished');
      message = t(key, { title: titleOf(runs[0].session_id) });
    } else {
      const prefix = missed ? 'toast.missedRuns' : 'toast.runs';
      const suffix = failed && completed ? 'Mixed' : failed ? 'Failed' : 'Finished';
      message = t(prefix + suffix, { n: runs.length, completed, failed });
    }
    // Success must never replace a recent failure summary.
    notify(message, { type, key: `run-results:${missed ? 'missed' : 'live'}:${type}` });
  }

  /** @param {RunNotice[]} records */
  function add(records, { missed = false } = {}) {
    const fresh = records.filter((r) =>
      (r.status === 'completed' || r.status === 'failed') && remember(r));
    if (!fresh.length) return false;
    if (missed) {
      show(fresh, true);
    } else {
      pending.push(...fresh);
      // Fixed window: a busy stream cannot postpone feedback indefinitely.
      if (timer === null) timer = setTimeout(() => {
        const batch = pending;
        pending = [];
        timer = null;
        show(batch, false);
      }, 1000);
    }
    return true;
  }

  return { add, remember };
}
