// The queue of messages that wait for a run to complete (send-queue.js, #308):
// when its head goes, and every ordering of the signals that wake it.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSendQueue } from '../../../lovia/web/static/js/send-queue.js';

/** A scripted server and page: tests set `live`, `records`, `ready`, and
 * queue up what each send answers; `host` overrides any of its hooks.
 * @param {Partial<import('../../../lovia/web/static/js/send-queue.js').QueueHost>} [host] */
function fixture(host = {}) {
  const s = {
    live: /** @type {Array<{ session_id: string, run_id: string }>} */ ([]),
    records: /** @type {Record<string, string>} */ ({}),
    ready: true,
    sent: /** @type {string[]} */ ([]),
    items: /** @type {any[]} */ ([]), // what each send was handed
    answers: /** @type {any[]} */ ([]),
  };
  const q = createSendQueue({
    liveRuns: async () => s.live,
    runRecord: async (id) => (id in s.records ? { status: s.records[id] } : null),
    ready: () => s.ready,
    send: async (_sid, item) => {
      s.sent.push(item.text);
      s.items.push(item);
      const answer = s.answers.shift() ?? { runId: `run-${item.text}` };
      return typeof answer === 'function' ? answer() : answer;
    },
    changed: () => {},
    ...host,
  });
  return { s, q };
}

// A macrotask: every pending pass of the queue has run by then.
const settled = () => new Promise((r) => setTimeout(r, 0));
/** Wake the queue, as an event would, and let it act. A wake-up during a
 * pass only marks it to run again, so awaiting `pump` itself isn't enough. */
const wake = async (/** @type {any} */ q) => {
  void q.pump('c');
  await settled();
};

test('waits for its run, then sends once it completed', async () => {
  const { s, q } = fixture();
  s.live = [{ session_id: 'c', run_id: 'R' }];
  s.records.R = 'running';
  q.enqueue('c', 'A', 'R');
  await settled();
  assert.deepEqual(s.sent, []);

  s.live = [];
  s.records.R = 'completed';
  await wake(q);
  assert.deepEqual(s.sent, ['A']);
  assert.deepEqual(q.view('c').items, []);
});

test('a local run end before the outcome is written waits for run_finished', async () => {
  const { s, q } = fixture();
  s.live = [{ session_id: 'c', run_id: 'R' }];
  q.enqueue('c', 'A', 'R');
  // The stream closed (run-end), the record still says running.
  s.live = [];
  s.records.R = 'running';
  await wake(q);
  assert.deepEqual(s.sent, []);
  // The finish lands; run_finished wakes the queue.
  s.records.R = 'completed';
  await wake(q);
  assert.deepEqual(s.sent, ['A']);
});

test('a run that did not complete pauses the queue', async () => {
  for (const status of ['failed', 'cancelled', 'interrupted']) {
    const { s, q } = fixture();
    s.records.R = status;
    q.enqueue('c', 'A', 'R');
    await settled();
    assert.deepEqual(s.sent, []);
    assert.equal(q.view('c').paused, 'failed');
    await wake(q); // stays put until the user acts
    assert.deepEqual(s.sent, []);
  }
});

test('a run the user stopped pauses the queue even if it ended completed (#322)', async () => {
  const { s, q } = fixture();
  s.live = [{ session_id: 'c', run_id: 'R' }];
  s.records.R = 'running';
  q.enqueue('c', 'A', 'R');
  await settled();
  q.markStopped('R'); // Stop pressed during the final model call…
  s.live = [];
  s.records.R = 'completed'; // …which finished anyway
  await wake(q);
  assert.deepEqual(s.sent, []);
  assert.equal(q.view('c').paused, 'failed');
  q.sendNow('c'); // the user's way on, as after any stop
  await settled();
  assert.deepEqual(s.sent, ['A']);
});

test('no record for a run that is gone is not a success', async () => {
  const { s, q } = fixture();
  q.enqueue('c', 'A', 'R');
  await settled();
  assert.deepEqual(s.sent, []);
  assert.equal(q.view('c').paused, 'failed');
});

test('FIFO: the next waits on the run the previous one started', async () => {
  const { s, q } = fixture();
  s.live = [{ session_id: 'c', run_id: 'R' }];
  q.enqueue('c', 'A', 'R');
  q.enqueue('c', 'B', 'R');
  s.live = [];
  s.records.R = 'completed';
  s.answers = [
    () => {
      s.live = [{ session_id: 'c', run_id: 'RA' }]; // live once admitted
      return { runId: 'RA' };
    },
  ];
  await wake(q);
  assert.deepEqual(s.sent, ['A']);
  // A's run failed: B must not go on its own.
  s.live = [];
  s.records.RA = 'failed';
  await wake(q);
  assert.deepEqual(s.sent, ['A']);
  assert.equal(q.view('c').paused, 'failed');
  assert.deepEqual(q.view('c').items.map((i) => i.text), ['B']);
});

test('FIFO: after a completed run the next goes too', async () => {
  const { s, q } = fixture();
  s.records.R = 'completed';
  s.records['run-A'] = 'completed';
  q.enqueue('c', 'A', 'R');
  q.enqueue('c', 'B', 'R');
  await settled();
  await wake(q);
  assert.deepEqual(s.sent, ['A', 'B']);
});

test('409 then the other run ends: the queue asks afresh, no event needed', async () => {
  const { s, q } = fixture();
  s.records.R = 'completed';
  // The first send loses to a run that had already finished by the time the
  // refusal is handled — its run_finished is gone. Asking afresh sends.
  s.answers = ['busy'];
  q.enqueue('c', 'A', 'R');
  await settled();
  await settled();
  assert.deepEqual(s.sent, ['A', 'A']);
  assert.deepEqual(q.view('c').items, []);
});

test('409 while the other run still goes: wait for it, then send', async () => {
  const { s, q } = fixture();
  s.records.R = 'completed';
  s.answers = [
    () => {
      s.live = [{ session_id: 'c', run_id: 'X' }]; // a schedule fired into the chat
      return 'busy';
    },
  ];
  q.enqueue('c', 'A', 'R');
  await settled();
  await settled();
  assert.deepEqual(s.sent, ['A']);
  s.live = []; // X ends; its run_finished wakes the queue
  await wake(q);
  assert.deepEqual(s.sent, ['A', 'A']);
});

test('a send with no answer pauses as unknown; send now forces the head', async () => {
  const { s, q } = fixture();
  s.records.R = 'completed';
  s.answers = ['unknown'];
  q.enqueue('c', 'A', 'R');
  q.enqueue('c', 'B', 'R');
  await settled();
  assert.deepEqual(s.sent, ['A']);
  assert.equal(q.view('c').paused, 'unknown');
  await wake(q);
  assert.deepEqual(s.sent, ['A']); // never resent on its own

  s.answers = [{ runId: 'RA' }];
  q.sendNow('c');
  await settled();
  assert.deepEqual(s.sent, ['A', 'A']);
  assert.deepEqual(q.view('c').items.map((i) => i.text), ['B']); // waits on RA
});

test('send now still waits for an idle chat: no resend loop on 409', async () => {
  const { s, q } = fixture();
  s.records.R = 'failed';
  q.enqueue('c', 'A', 'R');
  await settled();
  assert.equal(q.view('c').paused, 'failed');

  // Another run takes the chat just as the forced send goes out.
  s.answers = [
    () => {
      s.live = [{ session_id: 'c', run_id: 'X' }];
      return 'busy';
    },
  ];
  q.sendNow('c');
  await settled();
  await settled();
  assert.deepEqual(s.sent, ['A']); // one try, then it waits on X
  await wake(q);
  assert.deepEqual(s.sent, ['A']);

  s.live = []; // X ends: its run_finished wakes the queue
  await wake(q);
  assert.deepEqual(s.sent, ['A', 'A']);
  assert.deepEqual(q.view('c').items, []);
});

test('send now waits for a run already on the chat', async () => {
  const { s, q } = fixture();
  s.records.R = 'cancelled';
  q.enqueue('c', 'A', 'R');
  await settled();
  s.live = [{ session_id: 'c', run_id: 'X' }]; // e.g. a schedule fired
  q.sendNow('c');
  await settled();
  assert.deepEqual(s.sent, []);
  s.live = [];
  await wake(q);
  assert.deepEqual(s.sent, ['A']);
});

test('a refusal keeps the item and pauses', async () => {
  const { s, q } = fixture();
  s.records.R = 'completed';
  s.answers = [{ refused: 'too many concurrent runs' }];
  q.enqueue('c', 'A', 'R');
  await settled();
  assert.equal(q.view('c').paused, 'failed');
  assert.deepEqual(q.view('c').items.map((i) => i.text), ['A']);
});

test('nothing goes while the chat is not open and idle here', async () => {
  const { s, q } = fixture();
  s.records.R = 'completed';
  s.ready = false;
  q.enqueue('c', 'A', 'R');
  await wake(q);
  assert.deepEqual(s.sent, []);
  s.ready = true; // switched back, the view settled
  await wake(q);
  assert.deepEqual(s.sent, ['A']);
});

test('wake-ups during a pass coalesce: one send in flight', async () => {
  const { s, q } = fixture();
  s.records.R = 'completed';
  /** @type {(v: any) => void} */
  let admit = () => {};
  s.answers = [() => new Promise((r) => (admit = r))];
  q.enqueue('c', 'A', 'R');
  q.enqueue('c', 'B', 'R');
  await settled();
  void q.pump('c');
  void q.pump('c');
  await settled();
  assert.deepEqual(s.sent, ['A']);
  assert.equal(q.view('c').sending, q.view('c').items[0].id);
  q.withdraw('c', q.view('c').items[0].id); // on its way: can't be withdrawn
  assert.equal(q.view('c').items.length, 2);

  s.live = [{ session_id: 'c', run_id: 'RA' }];
  admit({ runId: 'RA' });
  await settled();
  await settled();
  assert.deepEqual(s.sent, ['A']); // B waits on RA, which is live
});

test('withdraw hands back the item and the run its queue waited on', async () => {
  const { s, q } = fixture();
  s.live = [{ session_id: 'c', run_id: 'R' }];
  q.enqueue('c', 'A', 'R');
  q.enqueue('c', 'B', 'R');
  await settled();
  const [a] = q.view('c').items;
  assert.deepEqual(q.withdraw('c', a.id), { item: a, after: 'R' });
  assert.equal(q.withdraw('c', a.id), null); // already out
  assert.deepEqual(q.view('c').items.map((i) => i.text), ['B']);
});

test('a steer no run took goes back to the head, after its old run', async () => {
  const { s, q } = fixture();
  s.live = [{ session_id: 'c', run_id: 'R' }];
  s.records.R = 'running';
  q.enqueue('c', 'A', 'R');
  q.enqueue('c', 'B', 'R');
  await settled();
  const taken = q.withdraw('c', q.view('c').items[1].id);
  assert.ok(taken);
  // The inject raced R's end: B rejoins at the head and waits on R as before.
  q.putBack('c', taken.item.text, taken.after);
  assert.deepEqual(q.view('c').items.map((i) => i.text), ['B', 'A']);
  s.live = [];
  s.records.R = 'completed';
  await wake(q);
  assert.deepEqual(s.sent, ['B']);
});

test('put back into an emptied queue still honours a run that failed', async () => {
  const { s, q } = fixture();
  s.live = [{ session_id: 'c', run_id: 'R' }];
  q.enqueue('c', 'A', 'R');
  await settled();
  const taken = q.withdraw('c', q.view('c').items[0].id);
  assert.ok(taken);
  assert.deepEqual(q.chats(), []);
  s.live = [];
  s.records.R = 'failed';
  q.putBack('c', taken.item.text, taken.after);
  await settled();
  assert.deepEqual(s.sent, []);
  assert.equal(q.view('c').paused, 'failed');
});

test('a steer with no answer goes back paused as unknown, never resent alone', async () => {
  const { s, q } = fixture();
  s.live = [{ session_id: 'c', run_id: 'R' }];
  q.enqueue('c', 'A', 'R');
  q.enqueue('c', 'B', 'R');
  await settled();
  const taken = q.withdraw('c', q.view('c').items[0].id);
  assert.ok(taken);
  // The inject's response was lost: R may already hold A.
  q.putBack('c', taken.item.text, taken.after, true);
  assert.equal(q.view('c').paused, 'unknown');
  s.live = [];
  s.records.R = 'completed';
  await wake(q);
  assert.deepEqual(s.sent, []); // R completing doesn't send A a second time

  q.sendNow('c'); // the user checked the chat and chose to send it
  await settled();
  assert.deepEqual(s.sent, ['A']);
});

for (const status of ['completed', 'failed', 'cancelled']) {
  test(`an unknown pause set during the check stands (R ${status})`, async () => {
    /** @type {() => void} */
    let release = () => {};
    const gate = new Promise((r) => (release = () => r(undefined)));
    // The check of R's outcome is still out when the steer's answer is lost.
    const { s, q } = fixture({
      runRecord: async () => {
        await gate;
        return { status };
      },
    });
    s.live = [{ session_id: 'c', run_id: 'R' }];
    q.enqueue('c', 'A', 'R');
    q.enqueue('c', 'B', 'R');
    await settled();
    const taken = q.withdraw('c', q.view('c').items[0].id); // A is steered…
    assert.ok(taken);
    s.live = []; // …R ends, and the queue starts its check
    void q.pump('c');
    await settled();
    q.putBack('c', taken.item.text, taken.after, true);
    release();
    await settled();
    assert.deepEqual(s.sent, []); // neither A (R may hold it) nor B behind it
    // Still "check the chat first", not a plain failure inviting a resend.
    assert.equal(q.view('c').paused, 'unknown');
  });
}

test('attachments ride their item through a 409 and a pause', async () => {
  const { s, q } = fixture();
  s.records.R = 'completed';
  const atts = [{ path: 'uploads/a.png', kind: 'image', name: 'a.png' }];
  s.answers = ['busy', { refused: 'too many concurrent runs' }];
  q.enqueue('c', 'look', 'R', atts);
  await settled();
  await settled();
  assert.equal(q.view('c').paused, 'failed');
  assert.deepEqual(q.view('c').items[0].attachments, atts); // kept while paused
  q.sendNow('c');
  await settled();
  assert.deepEqual(s.sent, ['look', 'look', 'look']);
  assert.ok(s.items.every((it) => it.attachments === atts));
  assert.deepEqual(q.view('c').items, []);
});

test('withdraw hands an item back with its attachments', async () => {
  const { s, q } = fixture();
  s.live = [{ session_id: 'c', run_id: 'R' }];
  const atts = [{ path: 'uploads/r.pdf', kind: 'file', name: 'r.pdf' }];
  q.enqueue('c', '', 'R', atts);
  await settled();
  const taken = q.withdraw('c', q.view('c').items[0].id);
  assert.deepEqual(taken?.item.attachments, atts);
});

test('withdrawing an unsure head lifts its pause from the next item (#323)', async () => {
  const { s, q } = fixture();
  s.records.R = 'completed';
  s.answers = ['unknown'];
  q.enqueue('c', 'A', 'R');
  q.enqueue('c', 'B', 'R');
  await settled();
  assert.equal(q.view('c').paused, 'unknown');
  q.withdraw('c', q.view('c').items[0].id); // the user checked: drop A
  await settled();
  assert.deepEqual(s.sent, ['A', 'B']); // B was never tried: it goes as usual
  assert.equal(q.view('c').paused, null);
});

test('withdrawing behind an unsure head keeps the pause', async () => {
  const { s, q } = fixture();
  s.records.R = 'completed';
  s.answers = ['unknown'];
  q.enqueue('c', 'A', 'R');
  q.enqueue('c', 'B', 'R');
  q.enqueue('c', 'C', 'R');
  await settled();
  q.withdraw('c', q.view('c').items[1].id); // B, not the head in doubt
  await settled();
  assert.equal(q.view('c').paused, 'unknown');
  assert.deepEqual(s.sent, ['A']);
});

test('withdrawing a head behind a failed run keeps the pause', async () => {
  const { s, q } = fixture();
  s.records.R = 'failed';
  q.enqueue('c', 'A', 'R');
  q.enqueue('c', 'B', 'R');
  await settled();
  q.withdraw('c', q.view('c').items[0].id);
  await settled();
  assert.equal(q.view('c').paused, 'failed'); // R still failed: B waits too
  assert.deepEqual(s.sent, []);
});

test('withdrawing the last item clears a pause', async () => {
  const { s, q } = fixture();
  s.records.R = 'failed';
  q.enqueue('c', 'A', 'R');
  await settled();
  assert.equal(q.view('c').paused, 'failed');
  q.withdraw('c', q.view('c').items[0].id);
  assert.deepEqual(q.view('c'), { items: [], paused: null, sending: null });
  assert.deepEqual(q.chats(), []);
});
