// @ts-check
// Message actions: 👍 / 👎 on answers (kept by the example's /feedback route
// under the answer's ref) and Quote on every message. Loaded by
// examples/35_web_feedback.py through ChatUI(scripts=["feedback.js"]).

const { lovia } = window;

const svg = (paths) =>
  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;

const ICONS = {
  up: svg(
    '<path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/>',
  ),
  down: svg(
    '<path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>',
  ),
};

/** The chat on screen's ratings: ref → 'up' | 'down'. */
let ratings = new Map();

// Fetch a chat's ratings when it comes on screen, then redraw the buttons so
// the ratings already given show pressed.
async function load(sessionId) {
  /** @type {Map<string, string>} */
  let next = new Map();
  if (sessionId) {
    const res = await lovia.fetch(`/feedback?session_id=${encodeURIComponent(sessionId)}`);
    if (res.ok) next = new Map(Object.entries(await res.json()));
  }
  if (lovia.state.sessionId !== sessionId) return; // the user moved on meanwhile
  ratings = next;
  lovia.refreshMessageActions();
}

lovia.on('session', ({ sessionId }) => void load(sessionId));

/**
 * @param {MessageRef} msg
 * @param {'up' | 'down'} rating
 */
async function rate(msg, rating) {
  const ref = /** @type {string} */ (msg.ref); // `when` shows the buttons only with one
  const next = ratings.get(ref) === rating ? null : rating; // clicking again takes it back
  const res = await lovia.fetch('/feedback', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ session_id: msg.sessionId, ref, rating: next, text: msg.text }),
  });
  if (!res.ok) throw new Error(lovia.t('acme.failed')); // shown as an error toast
  if (next) ratings.set(ref, next);
  else ratings.delete(ref);
  lovia.refreshMessageActions();
  if (next === 'down') lovia.toast(lovia.t('acme.thanks'));
}

for (const rating of /** @type {const} */ (['up', 'down'])) {
  lovia.registerMessageAction(`acme-${rating}`, {
    label: lovia.t(`acme.${rating}`),
    icon: ICONS[rating],
    // Answers only, once the server has named them: the ref is what the
    // rating is stored under, so a message without one can't be rated yet.
    when: (msg) => msg.role === 'assistant' && msg.ref !== null,
    pressed: (msg) => ratings.get(msg.ref ?? '') === rating,
    run: (msg) => rate(msg, rating),
  });
}

// Needs no ref: any settled message can be quoted.
lovia.registerMessageAction('acme-quote', {
  label: lovia.t('acme.quote'),
  icon: '❝',
  when: (msg) => msg.text !== '',
  run: (msg) => {
    const quoted = msg.text.split('\n').map((line) => `> ${line}`).join('\n');
    lovia.setDraft(`${quoted}\n\n`);
  },
});
