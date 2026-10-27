// The queue of messages that wait for a run to complete (send-queue.js, #308):
// when its head goes, and every ordering of the signals that wake it.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSendQueue } from '../../../lovia/web/static/js/send-queue.js';

/** A scripted server and page: tests set `live`, `records`, `ready`, and
 * queue up what each send answers. */
function fixture() {
  const s = {
    live: /** @type {Array<{ session_id: string, run_id: string }>} */ ([]),
    records: /** @type {Record<string, string>} */ ({}),
    ready: true,
    sent: /** @type {string[]} */ ([]),
    answers: /** @type {any[]} */ ([]),
  };
  const q = createSendQueue({
    liveRuns: async () => s.live,
    runRecord: async (id) => (id in s.records ? { status: s.records[id] } : null),
    ready: () => s.ready,
    send: async (_sid, text) => {
      s.sent.push(text);
      const answer = s.answers.shift() ?? { runId: `run-${text}` };
      return typeof answer === 'function' ? answer() : answer;
    },
    changed: () => {},
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
