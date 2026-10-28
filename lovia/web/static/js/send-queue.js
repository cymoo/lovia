// Messages queued to send once a run completes (#308): a FIFO per chat, held
// in this tab's memory. No DOM here — chat.js wires it to the page and draws
// its strip — so its races are testable in node (tests/web/js).
//
// A chat's queue waits on one run, `after`: the run that was live when its
// first item was queued, then the run each sent item started. Its head goes
// once `after` has ended `completed` and the chat is open and idle here; a run
// that ended any other way, or a send whose fate is unknown, pauses the queue
// for the user to send or withdraw by hand. Every wake-up — an enqueue, a run
// ending (seen locally or on /api/events), a switch back to the chat, a
// reconnect, a refused send — goes through one `pump`, which asks the server
// afresh each time, so no ordering of those signals can strand a queue.

/**
 * @typedef {{ id: number, text: string }} QueuedItem
 * @typedef {'failed' | 'unknown' | null} Paused `failed`: the run waited on
 *   didn't complete, or a send was refused. `unknown`: a send broke off
 *   before the server answered — it may have started a run.
 * @typedef {{ items: QueuedItem[], after: string | null, paused: Paused,
 *   force: boolean, pumping: boolean, again: boolean, sending: number | null }} Queue
 * @typedef {{ runId: string } | 'busy' | 'unknown' | { refused: string }} SendResult
 *   `runId`: admitted (the stream's `session` frame named the run).
 *   `busy`: 409 run_active — another run has the chat; nothing was sent.
 *   `refused`: any other refusal (its message); nothing was sent.
 *   `unknown`: no answer either way.
 * @typedef {object} QueueHost
 * @property {() => Promise<Array<{ session_id: string, run_id: string }>>} liveRuns
 * @property {(runId: string) => Promise<{ status: string } | null>} runRecord
 * @property {(sessionId: string) => boolean} ready The chat is open here and
 *   nothing streams in it.
 * @property {(sessionId: string, text: string) => Promise<SendResult>} send
 *   Send `text` with busy="reject"; resolves on admission or refusal.
 * @property {(sessionId: string) => void} changed
 */

/** @param {QueueHost} host */
export function createSendQueue(host) {
  /** @type {Map<string, Queue>} */
  const queues = new Map();
  let seq = 0;

  /** @param {string} sid */
  function queue(sid) {
    let q = queues.get(sid);
    if (!q) {
      q = {
        items: [],
        after: null,
        paused: null,
        force: false,
        pumping: false,
        again: false,
        sending: null,
      };
      queues.set(sid, q);
    }
    return q;
  }

  /** @param {string} sid */
  function settle(sid) {
    const q = queues.get(sid);
    if (q && !q.items.length && !q.pumping) queues.delete(sid);
    host.changed(sid);
  }

  /**
   * Whether the head may go: `go`, `wait` (a run still has the chat, or the
   * one waited on hasn't its outcome written yet), or `stop` (that run didn't
   * complete). `runId` null — a forced send — skips the outcome, never the
   * idle chat: a send into a busy chat would only bounce, again and again.
   * @param {string} sid
   * @param {string | null} runId
   * @returns {Promise<'go' | 'wait' | 'stop'>}
   */
  async function verdict(sid, runId) {
    const live = await host.liveRuns();
    if (live.some((r) => r.session_id === sid)) return 'wait';
    if (runId === null) return 'go';
    const rec = await host.runRecord(runId);
    // The record is written when the run starts, so none for a run that is no
    // longer live means it can't be vouched for: not a success.
    if (!rec) return 'stop';
    // Its stream may close before its outcome is written: run_finished follows.
    if (rec.status === 'running') return 'wait';
    return rec.status === 'completed' ? 'go' : 'stop';
  }

  /** @param {string} sid */
  async function pump(sid) {
    const q = queues.get(sid);
    if (!q || !q.items.length || q.paused) return;
    if (q.pumping) {
      q.again = true; // one pass at a time; this wake-up re-runs it after
      return;
    }
    q.pumping = true;
    try {
      do {
        q.again = false;
        if (!q.items.length || q.paused || !host.ready(sid)) return;
        // A check that can't reach the server waits for the next wake-up.
        const v = await verdict(sid, q.force ? null : q.after).catch(
          () => /** @type {const} */ ('wait'),
        );
        if (v === 'stop') {
          q.paused = 'failed';
          return;
        }
        if (v === 'wait') continue; // unless woken meanwhile, done for now
        if (!host.ready(sid)) return; // the page moved on during the check
        // …or the queue did: a steer whose answer was lost may have put its
        // item back, paused, while the check ran.
        if (q.paused || !q.items.length) return;
        const head = q.items[0];
        q.sending = head.id; // past withdrawing: it is on its way
        host.changed(sid);
        const res = await host.send(sid, head.text).finally(() => {
          q.sending = null;
        });
        if (res === 'busy') {
          // Ask afresh: another run took the chat, so the check waits for its
          // end (its run_finished wakes the queue) — or sends, if it is over.
          q.again = true;
        } else if (res === 'unknown') {
          q.paused = 'unknown';
        } else if ('refused' in res) {
          q.paused = 'failed';
        } else {
          q.items = q.items.filter((it) => it !== head);
          q.after = res.runId; // the next waits on the run this one started
          q.force = false;
        }
        host.changed(sid);
      } while (q.again);
    } finally {
      q.pumping = false;
      settle(sid);
    }
  }

  return {
    /**
     * Queue `text` for chat `sid`, after run `after` — the live run's id.
     * @param {string} sid
     * @param {string} text
     * @param {string} after
     */
    enqueue(sid, text, after) {
      const q = queue(sid);
      // A queue already waiting keeps its run: this item follows the others.
      if (!q.items.length) q.after = after;
      q.items.push({ id: ++seq, text });
      host.changed(sid);
      void pump(sid);
    },
    /**
     * Take item `id` out — never the one on its way — to drop, edit or steer.
     * Returns it with the run its queue waited on, for `putBack`.
     * @param {string} sid
     * @param {number} id
     * @returns {{ item: QueuedItem, after: string | null } | null}
     */
    withdraw(sid, id) {
      const q = queues.get(sid);
      const item = q?.items.find((it) => it.id === id);
      if (!q || !item || q.sending === id) return null;
      const after = q.after;
      q.items = q.items.filter((it) => it !== item);
      if (!q.items.length) q.paused = null;
      settle(sid);
      return { item, after };
    },
    /**
     * Return a withdrawn item to the head — a steer no run took. An emptied
     * queue waits on its old run again, so the item goes only if that run
     * completed, as it would have had it stayed. `unsure`: the steer got no
     * answer and may have landed, so the queue pauses as `unknown` instead.
     * @param {string} sid
     * @param {QueuedItem} item
     * @param {string | null} after
     * @param {boolean} [unsure]
     */
    putBack(sid, item, after, unsure = false) {
      const q = queue(sid);
      if (!q.items.length) q.after = after;
      q.items.unshift(item);
      if (unsure) q.paused = 'unknown';
      host.changed(sid);
      void pump(sid);
    },
    /** Send the head now, whatever its run did — the paused queue's way on. */
    sendNow(/** @type {string} */ sid) {
      const q = queues.get(sid);
      if (!q?.items.length) return;
      q.paused = null;
      q.force = true;
      host.changed(sid);
      void pump(sid);
    },
    pump,
    /** @param {string} sid */
    view(sid) {
      const q = queues.get(sid);
      return {
        items: q ? [...q.items] : [],
        paused: q?.paused ?? null,
        sending: q?.sending ?? null,
      };
    },
    /** Every chat with something queued. */
    chats: () => [...queues.keys()],
  };
}
