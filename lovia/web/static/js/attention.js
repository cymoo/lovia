// Runs that wait on the user: an approval card asked live, or a run suspended
// on a card, a question or a tool's own wait (GET /api/runs' `waits_on`). The
// page tells the user once per call, from whichever runs snapshot first shows
// it — a card asked live and then suspended is one wait, not two. No DOM here,
// so the bookkeeping is testable in node (tests/web/js).

/**
 * @typedef {{ kind: 'approval' | 'question' | 'call', id: string }} WaitsOn
 * @typedef {{ session_id: string, run_id: string, waits_on?: WaitsOn | null }} AttentionRun
 */

/** The wait's key, or null when the run waits on nobody. @param {AttentionRun} run */
export const waitKey = (run) => (run.waits_on ? `${run.run_id}:${run.waits_on.id}` : null);

/**
 * Tracks which waits the page has seen. The first snapshot only seeds it: a
 * chat that already waited when the page loaded is no news (its row says so).
 */
export function createAttention() {
  const seen = new Set();
  let primed = false;
  return {
    /**
     * The runs of `runs` that newly wait on the user.
     * @param {AttentionRun[]} runs
     * @returns {AttentionRun[]}
     */
    take(runs) {
      const fresh = [];
      for (const run of runs) {
        const key = waitKey(run);
        if (key === null || seen.has(key)) continue;
        seen.add(key);
        if (primed) fresh.push(run);
      }
      primed = true;
      // Bounded: only recent waits matter, and a key comes back at most once.
      while (seen.size > 512) seen.delete(seen.values().next().value);
      return fresh;
    },
  };
}
