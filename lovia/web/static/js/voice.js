// voice.js — voice input: record a take, have the server transcribe it, put
// the text in the composer.
//
// Loaded only where the server can transcribe (main.js, on
// features.transcription), and the rest of the page does not know it exists:
// it reaches the composer through chat.js's general exports (insertText,
// appendDraft, composerChat, gateSend), builds its own controls, and marks
// the composer form with data-voice for the stylesheet. Nothing imports it.
//
// A take is 'asking' (for the microphone) → 'recording' → 'working'
// (converting and transcribing) → gone, or 'failed' / 'dropped' on the way.
// It belongs to the chat it began in, by id. One take records at a time; a
// chat keeps at most one that is working, failed or dropped.

import { api } from './api.js';
import { appendDraft, composerChat, gateSend, insertText } from './chat.js';
import { t } from './i18n.js';
import { icon } from './icons.js';
import { lovia } from './lovia.js';
import { store } from './store.js';
import { toast } from './toast.js';
import { SILENCE_DB, captureBlocked, startCapture, toWav } from './voice-recorder.js';

// A press this long is "hold to talk": letting go ends the take. Shorter is
// a tap, and the take runs until ✓.
const HOLD_MS = 400;
// A slip of the finger, not a take.
const MIN_SECONDS = 0.3;
const MAX_SECONDS = 300;
// The service's own limit where the server knows one (enable()), else ours.
let maxSeconds = MAX_SECONDS;
// The clock counts down over the last stretch.
const WARN_SECONDS = 30;
// How long a cancelled take can still be brought back.
const RESTORE_MS = 5000;
const TICK_MS = 100;

/**
 * @typedef {object} Take
 * @property {string} chat The chat it began in (composerChat).
 * @property {'asking' | 'recording' | 'working' | 'failed' | 'dropped'} phase
 * @property {boolean} send Send once the text is in — ↑, not ✓.
 * @property {import('./voice-recorder.js').Capture} [capture]
 * @property {Promise<Blob>} [recorded] What the recorder kept, once it stops.
 * @property {Blob} [wav]
 * @property {AbortController} [abort]
 * @property {object} [run] The attempt in flight; a later one, or a cancel, replaces it.
 * @property {string} [error]
 * @property {boolean} [retry] Whether trying again could help.
 * @property {ReturnType<typeof setTimeout>} [timer]
 */

/** @type {Take | null} */
let live = null; // asking or recording
/** @type {Map<string, Take>} */
const kept = new Map(); // working, failed or dropped, by chat
/** The press that began the live take, while it is still down. @type {{ at: number, by: 'key' | 'pointer' } | null} */
let press = null;
/** When the pointer that began a take last came up (performance.now()). */
let pressEnded = -Infinity;
/** @type {ReturnType<typeof setInterval> | undefined} */
let ticker;
/** @type {number[]} */
let levels = [];
/** @type {(() => void)[]} */
let teardown = [];
/** @type {ReturnType<typeof build> | null} */
let ui = null;

const composer = () => /** @type {HTMLFormElement | null} */ (document.getElementById('composer'));
const shown = () => live ?? kept.get(composerChat() ?? '') ?? null;
const busyElsewhere = () => !!document.querySelector('dialog[open]') || !!lovia.state.view;

// ---- controls -------------------------------------------------------------

function button(cls, name, label, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = cls;
  b.innerHTML = icon(name, { size: 18, stroke: 2 });
  b.title = label;
  b.setAttribute('aria-label', label);
  b.addEventListener('click', onClick);
  return b;
}

function build() {
  const end = document.querySelector('[data-part="composer-end"]');
  const bar = document.querySelector('.lv-composer-bar');
  if (!end || !bar) return null;

  const mic = button('lv-voice-mic', 'mic', t('voice.mic'), (e) => {
    if (e.detail === 0) void start(null); // the keyboard; a pointer began on pointerdown
  });
  mic.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault(); // keep the focus where it was: this button is about to give way
    void start('pointer');
  });
  mic.addEventListener('contextmenu', (e) => e.preventDefault()); // a long press on touch
  end.insertBefore(mic, document.getElementById('stop'));

  const strip = document.createElement('div');
  strip.className = 'lv-voice-strip';
  strip.setAttribute('role', 'status');
  const cancelBtn = button('lv-voice-btn', 'x', t('voice.cancel'), () => cancel());
  const wave = document.createElement('canvas');
  wave.className = 'lv-voice-wave';
  wave.setAttribute('aria-hidden', 'true');
  const label = document.createElement('span');
  label.className = 'lv-voice-label';
  const clock = document.createElement('span');
  clock.className = 'lv-voice-clock';
  strip.append(cancelBtn, wave, label, clock);
  bar.insertBefore(strip, end);

  // ✓ appears where the microphone was. On touch, the click of the very tap
  // that began the take is hit-tested when the finger lifts — and lands here.
  // A click from the keyboard is always meant: no click count and no pointer
  // type (a touch click has no count either; an older Safari's has no type).
  const meant = (e) => (e.detail === 0 && !e.pointerType) || performance.now() - pressEnded > 350;
  const done = button('lv-voice-btn lv-voice-done', 'check', t('voice.done'), (e) => {
    if (meant(e)) void finish(false);
  });
  const send = button('lv-btn-send lv-voice-send', 'arrow-up', t('voice.send'), (e) => {
    if (meant(e)) void finish(true);
  });
  for (const b of [done, send]) {
    // Enter held down on the microphone repeats onto ✓, which took the focus.
    b.addEventListener('keydown', (e) => {
      if (e.repeat && (e.key === 'Enter' || e.key === ' ')) e.preventDefault();
    });
  }
  const text = (cls, key, onClick) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = `lv-btn lv-btn-ghost ${cls}`;
    b.textContent = t(key);
    b.addEventListener('click', onClick);
    return b;
  };
  const retry = text('lv-voice-retry', 'voice.retry', () => {
    const take = shown();
    if (take?.phase === 'failed') void work(take);
  });
  const restore = text('lv-voice-restore', 'voice.restore', () => {
    const take = shown();
    if (take?.phase !== 'dropped') return;
    clearTimeout(take.timer);
    void work(take);
  });
  end.append(done, send, retry, restore);

  return { mic, strip, cancelBtn, wave, label, clock, done, send, retry, restore };
}

function render() {
  const form = composer();
  if (!ui || !form) return;
  const take = shown();
  const phase = take?.phase;
  if (phase) form.dataset.voice = phase;
  else delete form.dataset.voice;
  ui.cancelBtn.hidden = phase === 'dropped';
  ui.wave.hidden = ui.clock.hidden = phase !== 'recording';
  ui.done.hidden = ui.send.hidden = phase !== 'recording';
  ui.retry.hidden = !(phase === 'failed' && take?.retry);
  ui.restore.hidden = phase !== 'dropped';
  ui.label.hidden = !phase || phase === 'recording';
  ui.label.textContent =
    phase === 'failed'
      ? (take?.error ?? '')
      : phase
        ? t({ asking: 'voice.asking', working: 'voice.working', dropped: 'voice.dropped' }[phase] ?? '')
        : '';
  ui.label.classList.toggle('lv-voice-error', phase === 'failed');
}

// ---- the meter and the clock ------------------------------------------------

const clockText = (seconds) => {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

function drawWave() {
  if (!ui) return;
  const { wave } = ui;
  const dpr = window.devicePixelRatio || 1;
  const w = wave.clientWidth;
  const h = wave.clientHeight;
  if (!w || !h) return;
  if (wave.width !== w * dpr || wave.height !== h * dpr) {
    wave.width = w * dpr;
    wave.height = h * dpr;
  }
  const g = wave.getContext('2d');
  if (!g) return;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  g.fillStyle = getComputedStyle(wave).color;
  const step = 5;
  const count = Math.floor(w / step);
  const recent = levels.slice(-count);
  recent.forEach((level, i) => {
    const bar = Math.max(2, level * h);
    const x = w - (recent.length - i) * step;
    g.fillRect(x, (h - bar) / 2, 3, bar);
  });
}

function tick() {
  const take = live;
  if (!ui || take?.phase !== 'recording' || !take.capture) return;
  const seconds = (performance.now() - take.capture.startedAt) / 1000;
  if (seconds >= maxSeconds) {
    void finish(false); // into the composer, never sent: nobody chose to
    return;
  }
  const warn = seconds >= maxSeconds - WARN_SECONDS;
  ui.clock.textContent = warn ? `-${clockText(maxSeconds - seconds + 1)}` : clockText(seconds);
  ui.clock.classList.toggle('lv-voice-warn', warn);
  const level = take.capture.level();
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
    // A level that holds still: every bar the same height, no scrolling.
    levels = new Array(200).fill(level);
  } else {
    levels.push(level);
    if (levels.length > 400) levels = levels.slice(-200);
  }
  drawWave();
}

// ---- a take's life -------------------------------------------------------------

function micError(err) {
  const name = err?.name;
  if (name === 'NotAllowedError' || name === 'SecurityError') return t('voice.denied');
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return t('voice.noDevice');
  if (name === 'NotReadableError') return t('voice.micBusy');
  return t('voice.micFailed', { reason: err?.message || name || '' });
}

/** @param {'key' | 'pointer' | null} by What is being held down, if anything is. */
async function start(by) {
  if (!ui || live || busyElsewhere()) return;
  const chat = composerChat({ mint: true });
  if (!chat || kept.has(chat)) return;
  const blocked = captureBlocked();
  if (blocked) {
    toast(t(blocked === 'insecure' ? 'voice.insecure' : 'voice.unsupported'), {
      type: 'error',
      timeout: 6000,
    });
    return;
  }
  /** @type {Take} */
  const take = { chat, phase: 'asking', send: false };
  live = take;
  press = by ? { at: performance.now(), by } : null;
  render();
  let capture;
  try {
    capture = await startCapture({
      lost: () => {
        if (live === take) void finish(false);
      },
    });
  } catch (err) {
    if (live !== take) return;
    live = press = null;
    render();
    toast(micError(err), { type: 'error', timeout: 6000 });
    return;
  }
  // Cancelled, or the chat was left, while the browser was still asking: a
  // late yes must not open the microphone on a page that moved on.
  if (live !== take) {
    capture.discard();
    return;
  }
  take.capture = capture;
  take.phase = 'recording';
  levels = [];
  render();
  // Enter on ✓ is "done"; the composer's own send key means the same while
  // a take records (the send gate below), so the focus needn't be guarded.
  ui.done.focus({ preventScroll: true });
  clearInterval(ticker);
  ticker = setInterval(tick, TICK_MS);
  tick();
}

/** The held key or pointer came up. */
function release(by) {
  if (press?.by !== by) return;
  const held = performance.now() - press.at;
  press = null;
  if (by === 'pointer') pressEnded = performance.now();
  // Let go while the browser still asks: that was a tap, and it records on.
  if (held >= HOLD_MS && live?.phase === 'recording') void finish(false);
}

/** End the recording and transcribe it. @param {boolean} send */
async function finish(send) {
  const take = live;
  if (take?.phase !== 'recording' || !take.capture) return;
  const { capture } = take;
  live = press = null;
  clearInterval(ticker);
  const seconds = (performance.now() - capture.startedAt) / 1000;
  take.capture = undefined;
  take.recorded = capture.stop();
  if (seconds < MIN_SECONDS) {
    render();
    return;
  }
  // A take cut short by leaving its chat only ever reaches the draft.
  take.send = send && take.chat === composerChat();
  await work(take);
}

/** Convert and transcribe, from the top: the first attempt, a retry, a restore. */
async function work(take) {
  const run = (take.run = {});
  const current = () => take.run === run && kept.get(take.chat) === take;
  take.phase = 'working';
  take.error = undefined;
  take.abort = new AbortController();
  kept.set(take.chat, take);
  render();
  try {
    if (!take.wav) {
      const { wav, peakDb } = await toWav(/** @type {Blob} */ (await take.recorded));
      if (!current()) return;
      if (peakDb < SILENCE_DB) {
        forget(take);
        toast(t('voice.silent'), { type: 'error', timeout: 6000 });
        return;
      }
      take.wav = wav;
    }
    const { text } = await api.transcribe(take.wav, { signal: take.abort.signal });
    if (!current()) return;
    forget(take);
    deliver(take, String(text ?? '').trim());
  } catch (err) {
    if (!current()) return;
    // A 413 that is not lovia's own (no code): a proxy in front stopped the
    // upload, and it will stop the next one just the same.
    const proxy = err?.status === 413 && !err?.code;
    take.phase = 'failed';
    take.error = proxy ? t('voice.proxyLimit') : t('voice.failed', { reason: err?.message || '' });
    take.retry = !proxy;
    // After a failure the text is looked at before it goes anywhere.
    take.send = false;
    render();
  }
}

function forget(take) {
  clearTimeout(take.timer);
  take.run = undefined;
  if (kept.get(take.chat) === take) kept.delete(take.chat);
  render();
}

function deliver(take, text) {
  if (!text) {
    toast(t('voice.empty'));
    return;
  }
  if (take.chat === composerChat()) {
    insertText(text);
    if (take.send) composer()?.requestSubmit();
    return;
  }
  const title = store.sessions.find((s) => s.id === take.chat)?.title || t('voice.newChat');
  if (appendDraft(take.chat, text)) toast(t('voice.toDraft', { title }));
  else toast(t('voice.noDraft'), { type: 'error' });
}

/** ✕ and Esc: stop, keeping the take a few seconds in case that was a slip. */
function cancel() {
  const take = shown();
  if (!take || take.phase === 'dropped') return;
  if (take === live) {
    live = press = null;
    clearInterval(ticker);
    if (!take.capture) {
      render(); // still asking: nothing was recorded
      return;
    }
    take.recorded = take.capture.stop();
    take.capture = undefined;
  }
  take.abort?.abort();
  take.run = undefined;
  take.send = false;
  take.phase = 'dropped';
  kept.set(take.chat, take);
  clearTimeout(take.timer);
  take.timer = setTimeout(() => {
    if (take.phase === 'dropped') forget(take);
  }, RESTORE_MS);
  render();
}

/** The chat or the view changed: no microphone stays open out of sight. */
function leave() {
  if (live?.phase === 'recording') void finish(false);
  else live = press = null;
  render();
}

// ---- wiring -----------------------------------------------------------------

function onKeyDown(e) {
  if (e.code === 'KeyM' && e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) {
    if (busyElsewhere()) return;
    // `code`, not `key`: an active input method reports key as "Process".
    // Prevented even on repeats: Edge and Firefox mute the tab on Ctrl+M.
    e.preventDefault();
    if (e.repeat) return;
    if (live?.phase === 'recording' && !press) void finish(false);
    else if (!live) void start('key');
    return;
  }
  if (e.key !== 'Escape' || e.defaultPrevented || e.isComposing) return;
  const phase = shown()?.phase;
  if (phase !== 'asking' && phase !== 'recording' && phase !== 'working') return;
  if (document.querySelector('dialog[open]')) return;
  // Only from the composer or neutral ground: Esc pressed in a menu or a
  // search box is meant for that.
  const ae = document.activeElement;
  if (ae && ae !== document.body && !composer()?.contains(ae)) return;
  e.preventDefault();
  cancel();
}

function onKeyUp(e) {
  if (e.code === 'KeyM' || e.key === 'Control') release('key');
}

function onBeforeUnload(e) {
  const pending = live?.phase === 'recording' || [...kept.values()].some((k) => k.phase === 'working');
  if (pending) e.preventDefault();
}

/** While a take records, the send key means ✓; while it transcribes, wait. */
function sendGate() {
  const take = shown();
  if (take === live && take?.phase === 'recording') {
    void finish(false);
    return true;
  }
  return take?.phase === 'working';
}

function listen(target, type, fn, opts) {
  target.addEventListener(type, fn, opts);
  teardown.push(() => target.removeEventListener(type, fn, opts));
}

/**
 * Put the microphone in the composer. Safe to call again, which is also how
 * a changed limit arrives. `seconds`: the longest take the service accepts.
 */
export function enable(seconds) {
  // A little short of the service's limit: the clock here and the length
  // of the file it gets don't agree to the frame.
  maxSeconds = Math.min(seconds ? seconds - 3 : MAX_SECONDS, MAX_SECONDS);
  if (ui) return;
  ui = build();
  if (!ui) return;
  listen(document, 'keydown', onKeyDown);
  listen(window, 'keyup', onKeyUp);
  listen(window, 'pointerup', () => release('pointer'));
  listen(window, 'pointercancel', () => release('pointer'));
  listen(window, 'blur', () => {
    release('key');
    release('pointer');
  });
  listen(window, 'beforeunload', onBeforeUnload);
  teardown.push(
    gateSend(sendGate),
    store.on('session-switched', leave),
    store.on('new-chat', leave),
    store.on('reset-chat-view', leave),
    store.on('session-deleted', (id) => {
      // Still recording in it: there is no chat left for the text to go to.
      if (live?.chat === id) {
        live.capture?.discard();
        live = press = null;
        clearInterval(ticker);
        render();
      }
      const take = kept.get(id);
      if (take) {
        take.abort?.abort();
        forget(take);
      }
    }),
    lovia.on('view', leave),
  );
  render();
}

/** Take it out again, dropping whatever was in flight. */
export function disable() {
  if (!ui) return;
  live?.capture?.discard();
  live = press = null;
  clearInterval(ticker);
  for (const take of kept.values()) {
    take.abort?.abort();
    clearTimeout(take.timer);
    take.run = undefined;
  }
  kept.clear();
  teardown.forEach((off) => off());
  teardown = [];
  Object.values(ui).forEach((el) => el.remove());
  ui = null;
  const form = composer();
  if (form) delete form.dataset.voice;
}
