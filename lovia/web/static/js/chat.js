// Chat streaming, SSE handling, message rendering.
import { applyStaticI18n, currentLang, t } from './i18n.js';
import { store } from './store.js';
import { toast } from './toast.js';
import { api, apiError, basePath, readSSE } from './api.js';
import { copyToClipboard, openImageLightbox } from './ui.js';
import { forkChat, loadSessions, updateSessionTitle } from './sessions.js';
import { renderMermaid } from './diagrams.js';
import { icon } from './icons.js';
import { enterToSend, followupsEnabled, openSettings } from './settings.js';
import { readFollowups, writeFollowups, dropFollowups } from './followup-cache.js';
import {
  cloneTemplate,
  formatDateTime,
  formatTimeSmart,
  highlightIn,
  isImagePath,
  renderMarkdownInto,
  sanitizedFragment,
  toDate,
} from './util.js';
import {
  STALE_TOOLS,
  buildToolNode,
  fillParams,
  findToolNode,
  refreshTool,
  setToolResult,
  setToolStatus,
  toolPath,
} from './tools.js';
import { BlockStream } from './stream-blocks.js';
import { createSendQueue } from './send-queue.js';
import { isStep, placeStep, removeStep, sealActivity } from './activity.js';
import { collectContext } from './lovia.js';
import { messageActionButtons, watchMessageActions } from './message-actions.js';
import {
  configureComponents,
  rerenderComponents,
  watchComponents,
} from './components.js';

// ---- Markdown & Highlighting -------------------------------------------
// marked / DOMPurify / hljs / mermaid arrive from CDN <script> tags and may
// be absent (offline, blocked CDN, SRI failure). Rendering helpers live in
// util.js (shared with the Files panel) and degrade to escaped text.
if (typeof marked !== 'undefined') marked.setOptions({ gfm: true, breaks: false });

// Exported for scripts/bench_stream_render.py, which times the real flush.
export function highlightCode(container, opts = {}) {
  highlightIn(container, opts); // shared cached hljs pass (util.js)
  // Copy buttons + language labels don't need hljs — keep them offline.
  addCodeBlockControls(container);
}

// Tell the Files panel a workspace tool wrote a file (decoupled via store).
function emitWorkspaceTouch(name, path) {
  if (name !== 'write_file' && name !== 'edit_file') return;
  if (path) store.emit('workspace-file-touched', { path });
}

// ---- Code block copy buttons -------------------------------------------
function addCodeBlockControls(container) {
  container.querySelectorAll('pre').forEach((pre) => {
    if (pre.querySelector('code.language-mermaid')) return; // diagram, not a code block
    if (pre.querySelector('.btn-copy-code')) return; // already added

    // Detect language from highlight.js class
    const code = pre.querySelector('code');
    let lang = '';
    if (code) {
      const classes = code.className.split(' ');
      for (const cls of classes) {
        if (cls.startsWith('language-') && cls !== 'language-') {
          lang = cls.replace('language-', '');
          break;
        }
      }
    }

    // Language label
    if (lang) {
      const label = document.createElement('span');
      label.className = 'code-lang';
      label.textContent = lang;
      pre.appendChild(label);
    }

    // Copy button
    const btn = document.createElement('button');
    btn.className = 'btn-copy-code';
    btn.type = 'button';
    btn.title = t('chat.copyCode');
    btn.innerHTML = `${icon('copy', { size: 12 })} ${t('chat.copyCode')}`;
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      let codeText = code?.textContent;
      if (!codeText) {
        // Fallback for a bare <pre>: strip the UI chrome (copy button,
        // language label) instead of pattern-matching on its label — which
        // broke in non-English UIs and on snippets ending with "Copy".
        const clone = pre.cloneNode(true);
        clone
          .querySelectorAll('.btn-copy-code, .code-lang')
          .forEach((n) => n.remove());
        codeText = clone.textContent || '';
      }
      const ok = await copyToClipboard(codeText.trimEnd());
      if (ok) {
        btn.innerHTML = `${icon('check', { size: 12 })} ${t('chat.copied')}`;
        btn.classList.add('copied');
        setTimeout(() => {
          btn.innerHTML = `${icon('copy', { size: 12 })} ${t('chat.copyCode')}`;
          btn.classList.remove('copied');
        }, 2000);
      }
    });
    pre.appendChild(btn);
  });
}

// Throttled streaming render: the first delta in a window arms the timer and
// later ones ride along, so a model that never pauses for 60ms (a local vLLM
// at 100+ tok/s) still repaints ~16×/s. A trailing debounce here (re-arming on
// every delta) starves until the turn ends and dumps the whole reply at once.
//
// The window widens with the cost of the last flush: a flush re-renders the
// whole reply (~0.9 ms per KB), so pacing the next one at 4× its cost keeps
// the render's share of the main thread near 20 % as the reply grows, at the
// price of coarser repaints. The 250 ms cap keeps the reply visibly alive
// rather than bounding the share — past ~60 ms per flush it climbs again.
let _renderTimer = null;
let _lastFlushMs = 0;
function scheduleRender() {
  if (_renderTimer) return;
  const delay = Math.min(250, Math.max(60, 4 * _lastFlushMs));
  _renderTimer = setTimeout(() => {
    _renderTimer = null;
    flushRender();
  }, delay);
}

// Nulling the id matters: a stale one would make scheduleRender() a no-op for
// every later delta. The cost memo resets too: every caller is a turn
// boundary, and the next reply starts small.
function cancelRender() {
  clearTimeout(_renderTimer);
  _renderTimer = null;
  _lastFlushMs = 0;
}

// True while the user has an active (non-collapsed) selection inside `node`.
function selectionInside(node) {
  const sel = document.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return false;
  return node.contains(sel.getRangeAt(0).commonAncestorContainer);
}

// True while an active selection touches any of `nodes`.
function selectionIntersects(nodes) {
  const sel = document.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return false;
  const range = sel.getRangeAt(0);
  return nodes.some((n) => range.intersectsNode(n));
}

// ---- Incremental streaming render ----------------------------------------
// A full re-render costs ~0.9 ms per KB of reply, every flush. The incremental
// path (parse side: stream-blocks.js) renders each block once it can no longer
// change and re-renders only the tail. One entry per streaming body: the parse
// state and a comment node separating the two regions. While `marker` is set
// the body is [committed blocks' nodes][marker][tail nodes], the committed
// nodes matching stream.committed one block at a time; only nodes after the
// marker are ever removed here. A full render (fallback, forced flush)
// replaces everything and clears `marker`. Keyed by the body element, so every
// path that opens a new body — a new turn, a snapshot, a discard — starts
// fresh with no bookkeeping.
const _incremental = new WeakMap();

function tailNodes(st) {
  const out = [];
  for (let n = st.marker.nextSibling; n; n = n.nextSibling) out.push(n);
  return out;
}

/** Take the incremental path if the parser allows it; false → render in full. */
function renderIncremental(body, text, opts) {
  let st = _incremental.get(body);
  if (!st) _incremental.set(body, (st = { stream: new BlockStream(), marker: null }));
  const r = st.stream.flush(text);
  if ('fallback' in r) {
    // The stream keeps its own state (a permanent fallback stays cheap; a
    // mismatch has already reset it); the DOM is about to be a full render.
    st.marker = null;
    return false;
  }
  if (!st.marker) {
    st.marker = document.createComment('tail');
    body.replaceChildren(st.marker);
  }
  for (const html of r.committed) body.insertBefore(sanitizedFragment(html, opts), st.marker);
  for (const n of tailNodes(st)) n.remove();
  body.appendChild(sanitizedFragment(r.tail, opts));
  return true;
}

// `force` bypasses the selection guard — end-of-turn flushes must land even
// mid-selection, or the bubble would freeze on stale content. A forced flush
// is also always a full render: the turn's final DOM is byte-for-byte what a
// reload renders, whatever the incremental path did.
function flushRender(force = false) {
  if (!store.body || !store.rawText) return;
  const body = store.body;
  const st = _incremental.get(body);
  // Replacing nodes destroys a selection in progress — copying from a
  // streaming reply would be impossible. Only the tail is replaced on the
  // incremental path, so a selection in committed blocks survives; skip the
  // flush when the selection reaches the region about to change. The next
  // delta (streaming keeps them coming) or the turn's forced flush repaints,
  // so no self-reschedule is needed while the selection is held.
  if (!force && selectionIntersects(st?.marker ? tailNodes(st) : [body])) return;
  const t0 = performance.now();
  const opts = { agent: store.agent };
  const incremental =
    !force &&
    typeof marked !== 'undefined' &&
    typeof DOMPurify !== 'undefined' &&
    renderIncremental(body, store.rawText, opts);
  if (!incremental) {
    // The parser just fell back (or this is the forced flush): the whole
    // body is about to be replaced, which the tail-only guard above did not
    // cover. Skipping here is safe — the stream's state already reads as
    // "nothing rendered": dead, or reset by the mismatch.
    if (!force && selectionIntersects([body])) return;
    if (force) _incremental.delete(body);
    renderMarkdownInto(body, store.rawText, opts);
  }
  body.dataset.raw = store.rawText; // what the DOM now shows, for copy/export
  // The last block is the only one that can still grow. Highlighting it is an
  // hljs cache miss on every flush (a 48 KB fence: ~30 ms of a ~31 ms flush),
  // so it waits until a block follows it or the turn's forced flush — plain
  // monospace until then. Any type: a list ending in a fence would otherwise
  // be re-highlighted whole. Committed blocks were highlighted once, when
  // they were committed; these passes skip them (data-highlighted, an
  // existing copy button, a swapped-in diagram).
  highlightCode(body, { skip: force ? null : body.lastElementChild });
  renderMermaid(body);
  // Layout is the largest part of a flush and would otherwise be paid in the
  // next frame (scrollDown reads scrollHeight in a rAF), outside this timing.
  // Forcing it here moves that work, not adds it: the frame then finds a
  // clean layout.
  void body.offsetHeight;
  // A forced flush is the turn's last render, and its callers have just
  // reset the memo (cancelRender) for the next turn — keep it that way. It
  // also highlights everything, so its cost isn't representative anyway.
  if (!force) _lastFlushMs = performance.now() - t0;
  scrollDown();
}

// ---- Templates ---------------------------------------------------------
function makeTurn(role, ts) {
  const node = cloneTemplate('tmpl-turn');
  node.classList.add(role);
  node.dataset.role = role;
  setTurnTimestamp(node, ts);
  return node;
}

function setTurnTimestamp(turn, ts = Date.now()) {
  if (!turn) return;
  // Compact display ("14:32" today), full form in the tooltip.
  turn.dataset.timestamp = formatTimeSmart(ts);
  turn.dataset.timestampFull = formatDateTime(ts, { seconds: true });
  const timestamp = turn.querySelector('.turn-footer .timestamp');
  if (timestamp) {
    timestamp.textContent = turn.dataset.timestamp;
    timestamp.title = turn.dataset.timestampFull;
  }
}

function contentText(content) {
  if (content == null) return '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content))
    return content.map((p) => (typeof p === 'string' ? p : p.text ?? '')).join('');
  return String(content);
}

function ensureFooter(bubble) {
  if (!bubble) return null;
  let footer = bubble.querySelector(':scope > .turn-footer');
  if (!footer) {
    footer = document.createElement('div');
    footer.className = 'turn-footer';
    const timestamp = document.createElement('span');
    timestamp.className = 'timestamp';
    footer.appendChild(timestamp);
  }
  const timestamp = footer.querySelector('.timestamp');
  const turn = bubble.closest('.turn');
  if (timestamp && turn?.dataset.timestamp) {
    timestamp.textContent = turn.dataset.timestamp;
    timestamp.title = turn.dataset.timestampFull || '';
  }
  bubble.appendChild(footer);
  return footer;
}

function appendBubbleContent(bubble, node) {
  if (!bubble || !node) return;
  // Content lands above the tail: the waiting dots (syncAwaiting), else the
  // footer — so a step still sees the activity group it extends.
  const tail = bubble.querySelector(':scope > .awaiting, :scope > .turn-footer');
  if (isStep(node)) {
    placeStep(bubble, node, tail);
    return;
  }
  // The reply's text has started: the steps before it fold away.
  if (node.classList.contains('body')) sealActivity(bubble);
  bubble.insertBefore(node, tail);
}

// ---- Date separators -----------------------------------------------------
// A quiet "Today / Yesterday / 2026-07-18" line whenever the calendar date
// changes between turns — long chats need anchors when scrolling back.
let _lastDateKey = null;

function dateLabel(ts) {
  const d = toDate(ts);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return t('chat.today');
  if (d.toDateString() === new Date(now.getTime() - 86400000).toDateString()) {
    return t('chat.yesterday');
  }
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// `anchorFirst` labels even the very first turn (history replay wants the
// anchor; a live chat's first message doesn't need a "Today" above it).
function maybeDateSeparator(transcriptEl, ts, { anchorFirst = false } = {}) {
  const key = toDate(ts).toDateString();
  if (key === _lastDateKey) return;
  const isFirst = _lastDateKey === null;
  _lastDateKey = key;
  if (isFirst && !anchorFirst) return;
  const sep = document.createElement('div');
  sep.className = 'date-separator';
  sep.textContent = dateLabel(ts);
  transcriptEl.appendChild(sep);
}

// ---- Edit & regenerate ---------------------------------------------------
// POST /sessions/{id}/rewind names a user turn by its server ref
// (dataset.ref) when the turn has one: it can't land on another message if
// the chat changed in another tab. A turn the server hasn't named yet (its
// run ended without one) falls back to its 0-based ordinal (dataset.userTurn).
// History renders assign that from the full entry list; live sends take the
// next number chronologically (queued bubbles get theirs only once the run
// confirms them, matching server transcript order).
let _userTurnCount = 0;

async function rewindTo(node, message) {
  const { ref, userTurn } = node.dataset;
  try {
    const res = await api.rewindSession(
      store.sessionId,
      ref ? { ref } : { user_turn: Number(userTurn) },
    );
    renderHistory(res.entries || []);
    hideContextMeter(); // the old fill describes a transcript that's gone
    _staleMeterSessions.add(store.sessionId); // …and so does its run record
  } catch (err) {
    toast(err.message || t('chat.rewindFailed'), { type: 'error' });
    return false;
  }
  document.getElementById('empty-state')?.remove();
  appendUserTurn(message);
  runStream(message); // fire-and-forget: its own finally restores the UI
  return true;
}

/**
 * Put the edit affordance on the LAST user turn, and nowhere else.
 *
 * Editing rewinds to just before the message and drops everything after it —
 * destructively, in the store. On the tail that is exactly "regenerate, but
 * with changes": it costs the one reply you are looking at. On an older
 * message the same hover icon silently discards every turn since, and the
 * tool calls it discards have already touched the workspace, so the surviving
 * transcript no longer describes what happened. Non-destructive editing of
 * arbitrary history needs branching (issue #150), not a confirm dialog.
 *
 * Mirrors updateRegenButton: same target turn, same lifecycle.
 */
function updateEditButton() {
  document.querySelectorAll('.btn-edit').forEach((b) => b.remove());
  if (!store.canRewind || store.streaming || !store.sessionId) return;
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return;
  // Confirmed turns only: a queued bubble has no ordinal to rewind to yet.
  const users = transcriptEl.querySelectorAll('.turn.user[data-user-turn]');
  const last = /** @type {HTMLElement | undefined} */ (users[users.length - 1]);
  if (last) addEditButton(last);
}

function addEditButton(node) {
  if (!store.canRewind) return;
  const bubble = node.querySelector('.bubble');
  const footer = bubble?.querySelector(':scope > .turn-footer');
  if (!footer || footer.querySelector('.btn-edit')) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn-edit';
  btn.title = t('chat.edit');
  btn.setAttribute('aria-label', t('chat.edit'));
  btn.innerHTML = icon('pencil', { size: 13 });
  btn.addEventListener('click', () => startEditUserTurn(node));
  footer.appendChild(btn);
}

// Swap the bubble's text for an inline editor; sending rewinds to just
// before this message and re-runs the edited text as a fresh turn.
function startEditUserTurn(node) {
  if (store.streaming) {
    toast(t('chat.editBusy'), { type: 'error' });
    return;
  }
  const bubble = node.querySelector('.bubble');
  const body = bubble?.querySelector('.body');
  if (!bubble || !body || node.dataset.userTurn == null) return;
  if (bubble.querySelector('.edit-area')) return; // already editing

  const wrap = document.createElement('div');
  wrap.className = 'edit-area';
  const ta = document.createElement('textarea');
  ta.value = body.textContent;
  const actions = document.createElement('div');
  actions.className = 'edit-actions';
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'btn btn-ghost btn-sm';
  cancelBtn.textContent = t('dialog.cancel');
  const sendBtn = document.createElement('button');
  sendBtn.type = 'button';
  sendBtn.className = 'btn btn-primary btn-sm';
  sendBtn.textContent = t('chat.send');
  actions.append(cancelBtn, sendBtn);
  wrap.append(ta, actions);

  const restore = () => {
    wrap.remove();
    body.style.display = '';
  };
  const commit = async () => {
    const text = ta.value.trim();
    if (!text) return;
    sendBtn.disabled = true;
    // Stopping a run is cooperative, so a rewind right after Esc waits for
    // whatever tool was mid-flight — say so rather than looking hung.
    sendBtn.textContent = t('chat.stopping');
    // On success the whole transcript re-renders (editor included); only a
    // failure leaves this editor alive — re-enable it for another try.
    if (!(await rewindTo(node, text))) {
      sendBtn.disabled = false;
      sendBtn.textContent = t('chat.send');
    }
  };
  cancelBtn.addEventListener('click', restore);
  sendBtn.addEventListener('click', commit);
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') restore();
    else if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      commit();
    }
  });

  body.style.display = 'none';
  bubble.insertBefore(wrap, body);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

// One floating regenerate action on the last assistant turn: rewind to just
// before the last user message and re-run it verbatim.
function updateRegenButton() {
  document.querySelector('.btn-regen')?.remove();
  if (!store.canRewind || store.streaming || !store.sessionId) return;
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return;
  const turns = transcriptEl.querySelectorAll('.turn');
  const last = turns[turns.length - 1];
  if (!last || !last.classList.contains('assistant')) return;
  const users = transcriptEl.querySelectorAll('.turn.user[data-user-turn]');
  const lastUser = /** @type {HTMLElement} */ (users[users.length - 1]);
  if (!lastUser) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn-regen';
  btn.title = t('chat.regenerate');
  btn.setAttribute('aria-label', t('chat.regenerate'));
  btn.innerHTML = icon('refresh-cw', { size: 13 });
  btn.addEventListener('click', () => {
    if (store.streaming) return;
    const text = lastUser.querySelector('.body')?.textContent?.trim();
    if (text) rewindTo(lastUser, text);
  });
  ensureFooter(last.querySelector('.bubble'))?.appendChild(btn);
}

// ---- Render helpers ----------------------------------------------------
/**
 * Append (or insert) a user message turn in the transcript.
 * @param {string} text
 * @param {{ queued?: boolean, before?: HTMLElement | null, attachments?: any[] | null }} [opts]
 * @returns {HTMLElement | null} The turn element, or null if the transcript is absent.
 */
export function appendUserTurn(text, { queued = false, before = null, attachments = null } = {}) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return null;
  if (!before) maybeDateSeparator(transcriptEl, Date.now());
  const node = makeTurn('user');
  if (queued) node.classList.add('queued');
  const bubble = node.querySelector('.bubble');
  if (text) {
    const body = document.createElement('div');
    body.className = 'body';
    body.textContent = text;
    appendBubbleContent(bubble, body);
  }
  if (attachments && attachments.length) {
    appendBubbleContent(bubble, makeAttachmentsBlock(attachments));
  }
  ensureFooter(bubble);
  if (!queued) {
    node.dataset.userTurn = String(_userTurnCount++);
    addMessageActions(bubble); // queued bubbles get their actions on confirm
    // No edit button here: it belongs to whichever turn ends up last, which
    // updateEditButton decides once the run settles.
  }
  if (before && before.parentNode === transcriptEl) {
    transcriptEl.insertBefore(node, before);
  } else {
    transcriptEl.appendChild(node);
  }
  // The reply above now ends before an unnamed turn: no branch until it's named.
  renderMessageActions(previousTurn(node)?.querySelector('.bubble'));
  scrollDown();
  return node;
}

/** @param {Element} node @returns {Element | null} The nearest `.turn` above. */
function previousTurn(node) {
  let prev = node.previousElementSibling;
  while (prev && !prev.classList.contains('turn')) prev = prev.previousElementSibling;
  return prev;
}

function startAssistantTurn(ts) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return {};
  const node = makeTurn('assistant', ts);
  node.classList.add('streaming');
  transcriptEl.appendChild(node);
  store.turnNode = node;
  store.bubble = node.querySelector('.bubble');
  store.body = null;
  store.rawText = '';
  store.toolNodes.clear();
  store.reasoningText = '';
  store.reasoningNode = null;
  store.reasoningStart = 0;
  store.reasoningEnd = 0;
  scrollDown();
  return { node, bubble: store.bubble };
}

/**
 * Re-open the transcript's trailing assistant turn for live writing.
 *
 * A re-attach renders the run's history-so-far and then has to put the live
 * continuation somewhere. Opening a fresh turn split one run into two bubbles
 * on screen — same run, two different renderings depending on whether the user
 * ever refreshed — and left the first half without the copy/regenerate actions
 * (which need a body to copy, and only ever land on the last turn). Continue
 * the run's own bubble instead.
 *
 * The snapshot only ever holds turns the server considers finished, so nothing
 * in the adopted bubble is still awaiting a live event; the in-flight turn
 * arrives as replayed events and renders through the normal live path.
 *
 * @returns {boolean} False when the tail isn't an assistant turn — a run whose
 *   current turn is its first. Callers fall back to `startAssistantTurn`.
 */
function resumeTailAssistantTurn() {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return false;
  const turns = transcriptEl.querySelectorAll('.turn');
  const node = /** @type {HTMLElement | undefined} */ (turns[turns.length - 1]);
  if (!node?.classList.contains('assistant')) return false;
  node.classList.add('streaming');
  store.turnNode = node;
  store.bubble = node.querySelector('.bubble');
  renderMessageActions(store.bubble); // none while it streams; settling redraws them
  // The rest starts clean: what's in the bubble is finished history, and the
  // continuation is a fresh model turn — its first delta opens a new body, its
  // thinking a new <details>, exactly as they render when nobody reconnected.
  store.body = null;
  store.rawText = '';
  store.reasoningText = '';
  store.reasoningNode = null;
  store.reasoningStart = 0;
  store.reasoningEnd = 0;
  scrollDown();
  return true;
}

function ensureBody() {
  if (!store.body && store.bubble) {
    store.body = document.createElement('div');
    store.body.className = 'body';
    appendBubbleContent(store.bubble, store.body);
    store.rawText = '';
  }
  return store.body;
}

function ensureReasoning() {
  if (!store.reasoningNode && store.bubble) {
    const details = document.createElement('details');
    details.className = 'reasoning';
    details.open = true;
    const summary = document.createElement('summary');
    summary.innerHTML = `<span class="reasoning-icon">💭</span><span class="reasoning-label">${t('chat.thinking')}</span>`;
    details.appendChild(summary);
    const content = document.createElement('div');
    content.className = 'reasoning-content';
    details.appendChild(content);
    // Append in stream order. A run shares one bubble across turns, so each
    // turn's reasoning must land after the prior turn's text/tools — inserting
    // at the top would stack every turn's thinking above the conversation.
    appendBubbleContent(store.bubble, details);
    store.reasoningNode = details;
  }
  return store.reasoningNode;
}

function finalizeReasoning() {
  if (!store.reasoningNode || !store.reasoningText) return;
  // Only collapse on the first call — subsequent calls (e.g. from
  // repeated text_delta events) must not reset the user's toggle.
  if (store.reasoningNode.classList.contains('done')) return;
  store.reasoningNode.open = false;
  store.reasoningNode.classList.add('done');
  const end = store.reasoningEnd || Date.now();
  const start = store.reasoningStart || end;
  const elapsed = (end - start) / 1000;
  const label = store.reasoningNode.querySelector('.reasoning-label');
  // Reasoning that arrived in one burst has no meaningful duration.
  if (label) {
    label.textContent = elapsed < 1 ? t('chat.thoughtDone') : t('chat.thought', { s: elapsed.toFixed(1) });
  }
}

// ---- Tool cards ----------------------------------------------------------
// Drawn and settled by tools.js; placing them in the live bubble is ours.
function appendTool(call) {
  if (!store.bubble) return;
  // A resumed run re-announces the calls it is about to drain (loop.py
  // `_drain_pending_calls`), and the session view already replayed those same
  // calls from the checkpoint — so the card can already be on screen. Adopt it
  // instead of drawing a second one, which left the replayed copy stranded
  // without a result forever. Rightly the run's job to re-announce: a consumer
  // attaching fresh has no prior state, so reconciling is this client's job.
  const existing = call.id ? findToolNode(call.id) : null;
  const node = existing || buildToolNode(call);
  if (!existing) {
    appendBubbleContent(store.bubble, node);
    refreshTool(node, true); // a tool view draws into the laid-out card
  }
  store.toolNodes.set(call.id, node);
  store.body = null;
  store.rawText = '';
  scrollDown();
}

function updateToolResult(id, result, isError, images) {
  const node = store.toolNodes.get(id);
  if (!node) return;
  setToolResult(node, result, isError, images, true);
  if (node.classList.contains('tool--ui')) scrollDown(); // a component adds height
}

function removeToolNode(id) {
  const node = store.toolNodes.get(id);
  if (node) { removeStep(node); store.toolNodes.delete(id); }
}

// ---- Todo plugin: a live checklist card --------------------------------
// Tool names whose calls render as a todo card instead of a tool bubble.
// Seeded with the default; renamed tools are learned from `todo` events.
const todoNames = new Set(['todo_write']);
// pending stays empty — its ring is drawn by `.todo-mark::before` in CSS.
const TODO_MARK = {
  completed: icon('check', { size: 13 }),
  in_progress: icon('loader-circle', { size: 13 }),
  pending: '',
};
const STICKY_SCROLL_PX = 160;
const USER_SCROLL_PAUSE_MS = 900;

// Parse a todo_write call's arguments into a todos array, or null.
function parseTodos(args) {
  try {
    const obj = JSON.parse(args);
    if (obj && Array.isArray(obj.todos)) {
      return obj.todos.map((todo) => ({
        content: todo.content ?? '',
        status: todo.status ?? 'pending',
        active_form: todo.active_form ?? null,
      }));
    }
  } catch { /* not a todo payload */ }
  return null;
}

function fillTodoCard(card, todos) {
  const total = todos.length;
  const done = todos.filter((todo) => todo.status === 'completed').length;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const expanded = !store.todoCollapsed;
  card.classList.toggle('complete', total > 0 && done === total);
  card.innerHTML =
    `<button class="todo-toggle" type="button" aria-expanded="${expanded}" title="${expanded ? t('todo.hide') : t('todo.show')}">` +
    `<span class="todo-title">${t('todo.plan')}</span>` +
    `<span class="todo-count">${done}/${total}</span>` +
    `<span class="todo-toggle-icon" aria-hidden="true">${expanded ? '-' : '+'}</span>` +
    '</button>' +
    '<div class="todo-content">' +
    `<div class="todo-bar"><div class="todo-bar-fill" style="width:${pct}%"></div></div>` +
    '<ul class="todo-list"></ul>' +
    '</div>';
  const toggle = card.querySelector('.todo-toggle');
  toggle?.addEventListener('click', () => setTodoCollapsed(!store.todoCollapsed));
  const ul = card.querySelector('.todo-list');
  for (const todo of todos) {
    const status = ['pending', 'in_progress', 'completed'].includes(todo.status)
      ? todo.status
      : 'pending';
    const li = document.createElement('li');
    li.className = `todo-item ${status}`;
    const label =
      status === 'in_progress' && todo.active_form ? todo.active_form : todo.content;
    const mark = document.createElement('span');
    mark.className = 'todo-mark';
    mark.innerHTML = TODO_MARK[status];
    const text = document.createElement('span');
    text.className = 'todo-text';
    text.textContent = label;
    li.append(mark, text);
    ul.appendChild(li);
  }
  return card;
}

function buildTodoCard(todos) {
  const card = document.createElement('div');
  card.className = 'todo-card';
  return fillTodoCard(card, todos);
}

function setTodoCollapsed(collapsed) {
  const panel = document.getElementById('todo-panel');
  store.todoCollapsed = collapsed;
  panel?.classList.toggle('collapsed', collapsed);
  const toggle = /** @type {HTMLElement | null} */ (panel?.querySelector('.todo-toggle'));
  const icon = panel?.querySelector('.todo-toggle-icon');
  toggle?.setAttribute('aria-expanded', String(!collapsed));
  if (toggle) toggle.title = collapsed ? t('todo.show') : t('todo.hide');
  if (icon) icon.textContent = collapsed ? '+' : '-';
}

function clearTodoPanel() {
  const panel = document.getElementById('todo-panel');
  if (panel) {
    panel.replaceChildren();
    panel.classList.add('hidden');
    panel.classList.remove('collapsed');
  }
  store.todoNode = null;
  // Same default as store.js: collapsed on phones, where the bottom-anchored
  // panel would otherwise cover the conversation (and approval buttons).
  store.todoCollapsed = !!(
    window.matchMedia && window.matchMedia('(max-width: 720px)').matches
  );
  store.todos = [];
}

function resetChatView() {
  _lastDateKey = null;
  _userTurnCount = 0;
  store.bubble = null;
  store.turnNode = null;
  store.body = null;
  store.rawText = '';
  store.toolNodes.clear();
  store.reasoningText = '';
  store.reasoningNode = null;
  store.reasoningStart = 0;
  store.reasoningEnd = 0;
  _queuedTurns = [];
  _pendingResend = [];
  clearTodoPanel();
}

// Create the session's todo panel on first sight, update it in place after.
function upsertTodoCard(todos) {
  const panel = document.getElementById('todo-panel');
  if (!panel) return;

  store.todos = todos;
  if (!todos.length) {
    clearTodoPanel();
    return;
  }

  panel.classList.remove('hidden');
  panel.classList.toggle('collapsed', store.todoCollapsed);
  if (store.todoNode && panel.contains(store.todoNode)) {
    fillTodoCard(store.todoNode, todos);
  } else {
    store.todoNode = buildTodoCard(todos);
    panel.replaceChildren(store.todoNode);
  }
  scrollDown();
}

// Per-chat tool allowlist (this browser tab only): approving with the
// "always allow" box ticked auto-approves that tool's future calls in the
// same chat — repeated identical approvals are pure friction. Deliberately
// NOT persisted: a reload starts asking again.
const _autoApprove = new Map(); // session id → Set<tool name>

function isAutoApproved(name) {
  return _autoApprove.get(store.sessionId)?.has(name) ?? false;
}

function rememberApproval(name) {
  let set = _autoApprove.get(store.sessionId);
  if (!set) {
    set = new Set();
    _autoApprove.set(store.sessionId, set);
  }
  set.add(name);
}

function appendApproval(call) {
  if (!store.bubble) return;
  if (isAutoApproved(call.name)) {
    // A quiet record instead of a card — the decision was already made.
    const note = document.createElement('div');
    note.className = 'approval-auto';
    note.textContent = t('approval.auto', { name: call.name });
    appendBubbleContent(store.bubble, note);
    api
      .approve({ session_id: store.sessionId, call_id: call.id, decision: 'approve' })
      .catch((err) => console.error(err));
    store.body = null;
    store.rawText = '';
    scrollDown();
    return;
  }
  setToolStatus(findToolNode(call.id), 'awaiting');
  store.emit('tool-awaiting');
  const node = cloneTemplate('tmpl-approval');
  const head = node.querySelector('.approval-head');
  if (head) head.textContent = t('approval.waiting');
  node.querySelector('.approve').textContent = t('approval.approve');
  node.querySelector('.decline').textContent = t('approval.deny');
  node.querySelector('.approval-name').textContent = call.name;
  fillParams(node.querySelector('.approval-args'), call.arguments);

  const always = document.createElement('label');
  always.className = 'approval-always';
  const box = document.createElement('input');
  box.type = 'checkbox';
  always.append(box, ` ${t('approval.always', { name: call.name })}`);
  node.querySelector('.approval-actions')?.before(always);

  const resolve = async (decision) => {
    node.classList.add('resolved');
    // A denial settles as an error result; an approval runs the call.
    if (decision === 'approve') setToolStatus(findToolNode(call.id), 'running');
    if (decision === 'approve' && box.checked) rememberApproval(call.name);
    always.remove();
    // Leave a record of which way it went instead of just dimming the card.
    const actions = node.querySelector('.approval-actions');
    if (actions) {
      const status = document.createElement('span');
      status.className = `approval-status ${decision}`;
      status.textContent =
        decision === 'approve' ? t('approval.approved') : t('approval.denied');
      actions.replaceChildren(status);
    }
    try {
      await api.approve({ session_id: store.sessionId, call_id: call.id, decision });
    } catch (err) { console.error(err); }
  };
  node.querySelector('.approve').addEventListener('click', () => resolve('approve'));
  node.querySelector('.decline').addEventListener('click', () => resolve('deny'));
  appendBubbleContent(store.bubble, node);
  store.body = null;
  store.rawText = '';
  scrollDown();
  // The run is now blocked on the user — say so past the aria-busy transcript,
  // and put the keyboard on the decision unless it's busy typing elsewhere.
  announce(t('a11y.approvalNeeded', { name: call.name }), { assertive: true });
  focusApprovalCard(node);
}

// Focus the Approve button so keyboard and screen-reader users land on the
// decision — but only from neutral ground: the page body, or the composer
// while it's empty (where Enter currently does nothing and the approval is
// the one thing waiting on the user). Any other focus — a search box, a
// dialog, a composer holding a draft — is somebody's work in progress.
function focusApprovalCard(node) {
  if (document.querySelector('dialog[open]')) return;
  const ae = document.activeElement;
  const neutral =
    !ae ||
    ae === document.body ||
    (ae === promptEl && !promptEl.value.trim());
  if (!neutral) return;
  /** @type {HTMLElement | null} */ (node.querySelector('.approve'))?.focus({
    preventScroll: true, // scrollDown() already owns the viewport
  });
}

function appendHandoff(from, to) {
  if (!store.bubble) return;
  const node = cloneTemplate('tmpl-handoff');
  node.querySelector('.handoff-text').textContent = `${from}  →  ${to}`;
  appendBubbleContent(store.bubble, node);
}

// ---- ask_human: interactive question card ------------------------------
// The run is parked on the ask_human tool call; this card answers it over
// POST /api/chat/answer. The plain tool card still shows the raw call — this
// card is the interaction, resolved either by the user or by the call's
// result event (an answer echoed back, or a timeout/cancel error).
const QUESTION_TOOL = 'ask_human';

function parseQuestion(args) {
  try {
    const obj = JSON.parse(args);
    if (obj && typeof obj.question === 'string') {
      const options = Array.isArray(obj.options)
        ? obj.options
            .map((o) => ({
              label: String(o?.label ?? ''),
              description: String(o?.description ?? ''),
            }))
            .filter((o) => o.label)
        : [];
      return {
        question: obj.question,
        options,
        multi: !!obj.multi_select && options.length > 0,
      };
    }
  } catch { /* not a question payload */ }
  return null;
}

function findQuestionNode(id) {
  const escaped = window.CSS?.escape ? CSS.escape(id) : id.replace(/["\\]/g, '\\$&');
  return /** @type {HTMLElement | null} */ (
    document.querySelector(`#transcript .question[data-call-id="${escaped}"]`)
  );
}

/** Freeze the card into its answered/expired end state. */
function settleQuestion(node, answer) {
  if (!node || node.classList.contains('resolved')) return;
  node.classList.add('resolved');
  const head = node.querySelector('.question-head');
  if (head) head.textContent = answer != null ? t('question.answered') : t('question.expired');
  node.querySelector('.question-options')?.remove();
  node.querySelector('.question-free')?.remove();
  if (answer != null && answer.trim()) {
    const echo = document.createElement('div');
    echo.className = 'question-answer';
    echo.textContent = answer;
    node.querySelector('.question-text')?.after(echo);
  }
}

/**
 * Build the interactive card for a parked ask_human call, or null when the
 * arguments don't parse as a question. Shared by the live event path
 * (appendQuestion) and the history/snapshot renderer, so a reloaded or
 * re-attached tab can still answer a question that is waiting server-side.
 */
function buildQuestionCard(call) {
  const q = parseQuestion(call.arguments);
  if (!q) return null; // malformed args — the plain tool card still shows the call

  const node = cloneTemplate('tmpl-question');
  node.dataset.callId = call.id || '';
  node.querySelector('.question-head').textContent = t('question.waiting');
  node.querySelector('.question-text').textContent = q.question;
  const optionsBox = node.querySelector('.question-options');
  const form = node.querySelector('.question-free');
  const input = /** @type {HTMLTextAreaElement} */ (
    node.querySelector('.question-input')
  );
  const send = node.querySelector('.question-send');
  send.textContent = t('question.send');
  input.placeholder = q.options.length
    ? t('question.placeholderWithOptions')
    : t('question.placeholder');
  input.setAttribute('aria-label', t('question.answerLabel'));

  const submit = async (answer) => {
    if (node.classList.contains('resolved')) return;
    try {
      const res = await api.answer({ session_id: store.sessionId, answer });
      if (res.ok) {
        settleQuestion(node, answer);
      } else if (res.status === 404) {
        // Nothing pending anymore (timed out / cancelled / answered from
        // another tab): freeze as expired; the stream's tool result tells
        // the story.
        settleQuestion(node, null);
      } else {
        throw new Error(String(res.status));
      }
    } catch (err) {
      // Transient failure (network, 5xx): the question is still parked
      // server-side, so keep the card interactive for a retry.
      console.error(err);
      toast(t('question.sendFailed'), { type: 'error' });
    }
  };

  const optionRow = (opt, el) => {
    const text = document.createElement('span');
    text.className = 'question-option-label';
    text.textContent = opt.label;
    el.append(text);
    if (opt.description) {
      const desc = document.createElement('span');
      desc.className = 'question-option-desc';
      desc.textContent = opt.description;
      el.append(desc);
    }
  };

  if (q.multi) {
    const boxes = [];
    for (const opt of q.options) {
      const row = document.createElement('label');
      row.className = 'question-option multi';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.value = opt.label;
      boxes.push(box);
      row.append(box);
      optionRow(opt, row);
      optionsBox.append(row);
    }
    const pick = document.createElement('button');
    pick.type = 'button';
    pick.className = 'btn btn-primary question-pick';
    pick.textContent = t('question.sendSelected');
    // Multi-select convention: chosen labels joined with newlines (labels
    // are schema-enforced single-line + unique, so the join is lossless).
    pick.addEventListener('click', () => {
      const chosen = boxes.filter((b) => b.checked).map((b) => b.value);
      if (chosen.length) submit(chosen.join('\n'));
    });
    optionsBox.append(pick);
  } else {
    for (const opt of q.options) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'question-option';
      btn.setAttribute(
        'aria-label',
        opt.description ? `${opt.label} — ${opt.description}` : opt.label,
      );
      optionRow(opt, btn);
      btn.addEventListener('click', () => submit(opt.label));
      optionsBox.append(btn);
    }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (text) submit(text);
  });
  input.addEventListener('keydown', (e) => {
    // Enter sends, Shift+Enter breaks the line; never during IME composition.
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      const text = input.value.trim();
      if (text) submit(text);
    }
  });
  return node;
}

function appendQuestion(call) {
  if (!store.bubble) return;
  // A resumed run re-announces the parked call (same reconciliation as
  // appendTool): if the card is already on screen, don't draw a second one.
  if (call.id && findQuestionNode(call.id)) return;
  const node = buildQuestionCard(call);
  if (!node) return;
  setToolStatus(findToolNode(call.id), 'awaiting');
  store.emit('tool-awaiting');
  appendBubbleContent(store.bubble, node);
  store.body = null;
  store.rawText = '';
  scrollDown();
  // The run is now parked on the user — same signals as an approval card.
  announce(t('a11y.questionAsked'), { assertive: true });
  focusQuestionCard(node);
}

// Same neutral-ground rule as focusApprovalCard: put the keyboard on the
// question only when nothing else holds focus (the page body, or an empty
// composer) — never yank it from a search box, dialog, or draft.
function focusQuestionCard(node) {
  if (document.querySelector('dialog[open]')) return;
  const ae = document.activeElement;
  const neutral =
    !ae ||
    ae === document.body ||
    (ae === promptEl && !promptEl.value.trim());
  if (!neutral) return;
  const target = node.querySelector('.question-option, .question-input');
  /** @type {HTMLElement | null} */ (target)?.focus({ preventScroll: true });
}

// Compact, human-readable token count: 950, 18.2k, 240k, 1.3M.
function formatTokens(n) {
  if (typeof n !== 'number' || !isFinite(n)) return null;
  if (n < 1000) return String(n);
  if (n < 100000) return `${(n / 1000).toFixed(1)}k`;
  if (n < 1000000) return `${Math.round(n / 1000)}k`;
  return `${(n / 1000000).toFixed(1)}M`;
}

// ---- Context ring --------------------------------------------------------
// How full the model's context is: the run's last_input_tokens IS the prompt
// the model just saw (usage.input_tokens sums every call's prompt, so it
// overstates fill on tool-looping runs — older records lack the field and
// fall back to it), and the agent advertises its window via
// AgentInfo.context_window. Hidden until usage is known; hides again on chat
// switches. Clicking the ring opens a detail popover (tokens, cache split,
// model).
let _lastUsage = null; // the most recent run's usage dict, for the popover

// The prompt size of the run's final model call — the context-fill numerator.
function contextFill(usage) {
  return usage?.last_input_tokens ?? usage?.input_tokens;
}

function updateContextMeter(usage) {
  const el = document.getElementById('context-ring');
  if (!el) return;
  const window_ = store.agents.find((a) => a.name === store.agent)?.context_window;
  const fill = contextFill(usage);
  if (!fill) {
    // null, or 0 — no prompt is empty, so 0 is a provider that reports no usage
    hideContextMeter();
    return;
  }
  _lastUsage = usage;
  _staleMeterSessions.delete(store.sessionId);
  // Unknown window (provider advertises none): a neutral ring with no fill —
  // the popover still serves the token/cache detail.
  const pct = window_ ? Math.min(100, Math.round((fill / window_) * 100)) : null;
  el.classList.remove('hidden');
  el.classList.toggle('nowin', pct == null);
  el.classList.toggle('warn', pct != null && pct >= 70 && pct < 90);
  el.classList.toggle('danger', pct != null && pct >= 90);
  const detail = pct != null
    ? t('context.meter', { used: formatTokens(fill), window: formatTokens(window_), pct })
    : t('context.meterNoWindow', { used: formatTokens(fill) });
  el.title = detail;
  el.setAttribute('aria-label', detail); // keep assistive tech in sync
  // pathLength="100" on the circle → the dash array speaks percentages.
  /** @type {SVGElement} */ (
    el.querySelector('.context-ring-fill')
  ).style.strokeDasharray = `${pct ?? 0} 100`;
  if (!document.getElementById('context-popover')?.hidden) fillContextPopover();
}

function hideContextMeter() {
  _lastUsage = null;
  document.getElementById('context-ring')?.classList.add('hidden');
  toggleContextPopover(false);
}

function fillContextPopover() {
  const rows = document.querySelector('#context-popover .context-popover-rows');
  if (!rows) return;
  const agent = store.agents.find((a) => a.name === store.agent);
  const window_ = agent?.context_window;
  const u = _lastUsage || {};
  const fill = contextFill(u);
  const pct = window_ && fill != null
    ? Math.min(100, Math.round((fill / window_) * 100))
    : null;
  const entries = [
    [t('ctx.context'), pct != null
      ? `${formatTokens(fill)} / ${formatTokens(window_)} · ${pct}%`
      : formatTokens(fill), true],
    [t('ctx.model'), agent?.model, false],
    [t('ctx.input'), formatTokens(u.input_tokens), false],
    [t('ctx.output'), formatTokens(u.output_tokens), false],
    [t('ctx.cacheRead'), formatTokens(u.cache_read_tokens), false],
    [t('ctx.cacheWrite'), formatTokens(u.cache_write_tokens), false],
  ];
  rows.replaceChildren(
    ...entries
      .filter(([, v]) => v != null)
      .map(([k, v, head]) => {
        const row = document.createElement('div');
        row.className = 'context-popover-row' + (head ? ' context-popover-head' : '');
        const key = document.createElement('span');
        key.className = 'k';
        key.textContent = k;
        const val = document.createElement('span');
        val.className = 'v';
        val.textContent = v;
        val.title = v;
        row.append(key, val);
        return row;
      }),
  );
}

function toggleContextPopover(open) {
  const pop = document.getElementById('context-popover');
  const ring = document.getElementById('context-ring');
  if (!pop || !ring) return;
  const show = open ?? pop.hidden;
  if (show) fillContextPopover();
  pop.hidden = !show;
  ring.setAttribute('aria-expanded', String(show));
}

/** Wire up the context-usage ring in the top bar and its details popover. */
export function initContextRing() {
  const ring = document.getElementById('context-ring');
  const pop = document.getElementById('context-popover');
  if (!ring || !pop) return;
  // A model switch refetches the agent list; the ring's denominator (the
  // window the NEXT message runs against) and the popover's model row must
  // follow it instead of showing the previous model's math.
  store.on('agents-loaded', () => {
    if (_lastUsage) updateContextMeter(_lastUsage);
  });
  ring.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleContextPopover();
  });
  document.addEventListener('click', (e) => {
    if (!pop.hidden && !pop.contains(/** @type {Node} */ (e.target))) toggleContextPopover(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !pop.hidden) toggleContextPopover(false);
  });
}

// Usage is a property of the conversation on screen — switching away clears
// it, and entering a chat restores it from the durable run record (so the
// ring survives reloads). Restore hooks 'render-history', which fires after
// 'sync-agent' — the window lookup needs the chat's own agent in place.
// Sessions rewound this page-view are skipped: their latest record describes
// a transcript that's gone; the next completed run un-marks them.
const _staleMeterSessions = new Set();

async function restoreContextMeter(sessionId) {
  if (!sessionId || _staleMeterSessions.has(sessionId)) return;
  let records = [];
  try {
    records = await api.runHistory({ session_id: sessionId, limit: 5 });
  } catch {
    return; // cosmetic — a failed restore just leaves the ring hidden
  }
  const usage = records.find((r) => r.usage)?.usage; // bad fires store none
  // Bail when superseded: the user switched on, or a live `done` beat us.
  if (!usage || store.sessionId !== sessionId || _lastUsage) return;
  updateContextMeter(usage);
}

store.on('session-switched', hideContextMeter);
store.on('render-history', () => restoreContextMeter(store.sessionId));

// Surface why compaction fired and how much it saved. Policy-agnostic: the
// numeric fields (tokens_before/after) ride at the top level and the policy
// authors its own `detail` bullets, so this renders any ContextPolicy's notice
// without knowing its internals; everything degrades gracefully if a field is
// absent. Shared by the live SSE path (target = the active assistant bubble) and
// history replay (target = the run's bubble, or the transcript for a boundary
// notice).
function appendContextCompacted(target, data) {
  if (!target || !data) return;
  const node = cloneTemplate('tmpl-context-compacted');
  const titleEl = node.querySelector('.context-title');
  if (titleEl) titleEl.textContent = t('context.compacted');
  if (data.reason) node.title = `reason: ${data.reason}`;

  // Trigger chip — reactive means we recovered from a provider context-overflow;
  // otherwise compaction fired proactively at the high-water mark.
  const trigger = node.querySelector('.context-trigger');
  if (data.reactive) {
    trigger.textContent = t('context.reactive');
    trigger.classList.add('context-trigger--reactive');
  } else {
    trigger.textContent = t('context.proactive');
    trigger.classList.add('context-trigger--proactive');
  }

  // Primary stat — tokens before → after, with the reduction percentage.
  const stats = node.querySelector('.context-stats');
  const before = formatTokens(data.tokens_before);
  const after = formatTokens(data.tokens_after);
  if (before && after) {
    const flow = document.createElement('span');
    flow.className = 'context-flow';
    flow.textContent = `${before} → ${after} tokens`;
    stats.appendChild(flow);
    const pct =
      data.tokens_before > 0
        ? Math.round((1 - data.tokens_after / data.tokens_before) * 100)
        : 0;
    if (pct !== 0) {
      const badge = document.createElement('span');
      badge.className = `context-badge${pct < 0 ? ' context-badge--grow' : ''}`;
      badge.textContent = pct < 0 ? `+${-pct}%` : `-${pct}%`;
      stats.appendChild(badge);
    }
  } else {
    stats.remove();
  }

  // Detail line — bullets the policy authored, rendered verbatim.
  const detail = node.querySelector('.context-detail');
  const bits = Array.isArray(data.detail) ? data.detail : [];
  if (bits.length) {
    detail.textContent = bits.join(' · ');
  } else {
    detail.remove();
  }

  // Full summary text, collapsed by default.
  if (data.summary) {
    const details = document.createElement('details');
    details.className = 'context-summary';
    const label = document.createElement('summary');
    label.textContent = t('context.summary');
    details.appendChild(label);
    const body = document.createElement('div');
    body.className = 'context-summary-body';
    body.textContent = data.summary;
    details.appendChild(body);
    node.appendChild(details);
  }

  appendBubbleContent(target, node);
}

// The error codes a person fixes in the model settings, not by waiting.
const SETTINGS_FIXABLE = new Set(['provider_auth', 'network']);
/** The code of the run's last `error` event, for the retry row's offer. */
let _lastErrorCode = '';

function appendRetry() {
  if (!store.bubble) return;
  const node = cloneTemplate('tmpl-retry');
  const btn = node.querySelector('.retry-btn');
  btn.textContent = t('chat.retry');
  btn.addEventListener('click', () => store.emit('retry'));
  if (store.canConfigureModels && SETTINGS_FIXABLE.has(_lastErrorCode)) {
    const settings = document.createElement('button');
    settings.type = 'button';
    settings.className = 'btn btn-ghost btn-sm';
    settings.textContent = t('chat.openModelSettings');
    settings.addEventListener('click', () => openSettings('models'));
    node.appendChild(settings);
  }
  appendBubbleContent(store.bubble, node);
  announce(t('a11y.runFailed'), { assertive: true });
}

// ---- Screen-reader announcements ----------------------------------------
// The transcript carries aria-busy while streaming (so 60 ms re-renders don't
// spam screen readers), which also mutes anything appended inside it — a
// pending approval or a finished reply would go unnoticed. These two live
// regions sit OUTSIDE the transcript for exactly that reason (index.html).
function announce(text, { assertive = false } = {}) {
  const el = document.getElementById(assertive ? 'sr-alert' : 'sr-status');
  if (!el) return;
  el.textContent = ''; // clear first so repeating the same text re-announces
  setTimeout(() => { el.textContent = text; }, 30);
}

// ---- Error humanizing ---------------------------------------------------
// Raw provider/network errors ("429 Too Many Requests", "Failed to fetch")
// mean nothing to most users. Map the recognizable ones onto a sentence that
// says what happened and what to do; the original text stays visible in
// small print — friendly must never mean information destroyed.
// The server classifies what it can (lovia/web/errors.py); the patterns below
// cover what it can't (quota wording) and servers that send no code.
/** @type {Record<string, string>} */
const ERROR_CODE_HINTS = {
  server_token: t('err.serverToken'),
  rate_limited: t('err.rateLimit'),
  provider_auth: t('err.auth'),
  overloaded: t('err.overloaded'),
  timeout: t('err.timeout'),
  // The server's `network` is lovia → provider; err.network (below) is the
  // browser → lovia leg.
  network: t('err.providerNetwork'),
};
/** @type {Array<[RegExp, string]>} */
const ERROR_HINTS = [
  [/rate.?limit|too many requests|\b429\b/i, t('err.rateLimit')],
  [/unauthorized|forbidden|api.?key|authenticat|\b401\b|\b403\b/i, t('err.auth')],
  [/quota|billing|insufficient|credit/i, t('err.quota')],
  [/overloaded|service unavailable|\b529\b|\b503\b/i, t('err.overloaded')],
  [/timed?.?out|timeout/i, t('err.timeout')],
  [/failed to fetch|networkerror|load failed|fetch failed/i, t('err.network')],
];

// The friendly sentence for a raw error, or null when it's unrecognized
// (callers then show the raw message alone).
/**
 * @param {unknown} message
 * @param {string} [code] the server's error code, when it sent one
 */
function humanizeError(message, code) {
  if (code && ERROR_CODE_HINTS[code]) return ERROR_CODE_HINTS[code];
  const msg = String(message ?? '');
  for (const [re, hint] of ERROR_HINTS) {
    if (re.test(msg)) return hint;
  }
  return null;
}

// A run-level error that the run itself recovers from — most commonly a tool
// raising, which lovia feeds back to the model to handle. Show it as a quiet
// inline notice (no Retry: re-sending the whole turn doesn't retry the tool,
// and the model usually copes on its own).
/**
 * @param {string} message
 * @param {string} [code]
 */
function appendErrorNotice(message, code) {
  if (!store.bubble) return;
  const note = document.createElement('div');
  note.className = 'error-notice';
  note.innerHTML = icon('triangle-alert', { size: 15, cls: 'error-notice-icon' });
  const main = document.createElement('div');
  main.className = 'error-notice-main';
  const hint = humanizeError(message, code);
  const head = document.createElement('div');
  head.textContent = hint ?? String(message);
  main.appendChild(head);
  if (hint) {
    // The raw error, one click away: friendly never means destroyed.
    const detail = document.createElement('details');
    detail.className = 'error-notice-detail';
    const summary = document.createElement('summary');
    summary.textContent = t('chat.errorDetails');
    const raw = document.createElement('div');
    raw.textContent = String(message);
    detail.append(summary, raw);
    main.appendChild(detail);
  }
  note.appendChild(main);
  appendBubbleContent(store.bubble, note);
  // Begin a fresh body so any recovery text doesn't merge into the pre-error one.
  store.body = null;
  store.rawText = '';
  scrollDown();
}

function cleanMarkdownForCopy(markdown) {
  const lines = markdown.trim().split('\n');
  let openFence = null;
  const cleaned = [];
  const markdownBoundary = /^(---+|\*\*\*+|___+|#{1,6}\s|[-*+]\s|\d+\.\s|>\s|\|.*\|)/;
  const nextNonEmpty = (start) => {
    for (let i = start; i < lines.length; i++) {
      const text = lines[i].trim();
      if (text) return text;
    }
    return '';
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();
    const match = trimmed.match(/^(```+|~~~+)(.*)$/);
    const marker = match?.[1];
    if (!marker) {
      cleaned.push(line);
      continue;
    }

    if (!openFence) {
      const info = (match?.[2] || '').trim();
      const next = nextNonEmpty(i + 1);
      if (!info && (!next || markdownBoundary.test(next))) continue;
      openFence = marker;
      cleaned.push(line);
    } else if (
      marker[0] === openFence[0] &&
      marker.length >= openFence.length
    ) {
      cleaned.push(line);
      openFence = null;
    } else {
      cleaned.push(line);
    }
  }
  if (openFence && /^```+\s*$/.test(cleaned[cleaned.length - 1]?.trim() || '')) {
    cleaned.pop();
  }
  return cleaned.join('\n').trim();
}

// A bubble's text as Markdown: what copy puts on the clipboard.
function bubbleMarkdown(bubble) {
  const bodies = Array.from(bubble.children).filter((node) =>
    node.classList?.contains('body')
  );
  return cleanMarkdownForCopy(bodies
    .map((body) => body.dataset.raw || body.textContent || '')
    .map((text) => text.trim())
    .filter(Boolean)
    .join('\n\n'));
}

// The footer's copy button, then the deployer's message actions.
function addMessageActions(bubble) {
  if (!bubble) return;
  for (const node of bubble.querySelectorAll(':scope > .btn-copy, :scope > .turn-footer > .btn-copy')) {
    node.remove();
  }
  const footer = ensureFooter(bubble);
  addCopyButton(footer, bubbleMarkdown(bubble));
  renderMessageActions(bubble);
}

function addCopyButton(footer, markdown) {
  if (!markdown) return;
  const btn = cloneTemplate('tmpl-copy-btn');
  btn.addEventListener('click', async () => {
    const ok = await copyToClipboard(markdown);
    if (ok) {
      btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
      btn.classList.add('copied');
      setTimeout(() => {
        btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
        btn.classList.remove('copied');
      }, 1500);
    }
  });
  footer?.appendChild(btn);
}

/**
 * A turn as message actions see it. A reply carries the ref of the user
 * message it answers: the nearest confirmed user turn above it.
 * @param {HTMLElement} turn
 * @returns {MessageRef}
 */
function describeMessage(turn) {
  const role = turn.classList.contains('user') ? 'user' : 'assistant';
  let user = /** @type {Element | null} */ (turn);
  while (user && !user.matches('.turn.user:not(.queued)')) user = user.previousElementSibling;
  let ref = /** @type {HTMLElement | null} */ (user)?.dataset.ref ?? null;
  if (!user && turn.dataset.question) {
    // Its question is above the rendered window: the history still has it.
    const cached = _historyEntries.filter((e) => e.role === 'user');
    ref = cached[Number(turn.dataset.question)]?.ref ?? null;
  }
  const bubble = turn.querySelector('.bubble');
  return Object.freeze({
    sessionId: store.sessionId,
    ref,
    role,
    text: bubble ? bubbleMarkdown(bubble) : '',
  });
}

// (Re)draw a settled turn's message actions, right after its copy button. A
// queued or still-streaming turn gets none yet; its settling draws them.
function renderMessageActions(bubble) {
  const turn = /** @type {HTMLElement | null} */ (bubble?.closest('.turn'));
  const footer = bubble?.querySelector(':scope > .turn-footer');
  if (!turn || !footer) return;
  renderBranchButton(turn, footer);
  let box = footer.querySelector(':scope > .msg-actions');
  const settled = !turn.matches('.queued, .streaming');
  const buttons = settled ? messageActionButtons(() => describeMessage(turn), turn) : [];
  if (!buttons.length) {
    box?.remove();
    return;
  }
  if (!box) {
    box = document.createElement('span');
    box.className = 'msg-actions';
    box.setAttribute('data-part', 'message-actions');
  }
  box.replaceChildren(...buttons);
  const anchor =
    footer.querySelector(':scope > .btn-copy') ?? footer.querySelector(':scope > .timestamp');
  if (anchor) anchor.after(box);
  else footer.prepend(box);
}

/**
 * Where a branch from this reply cuts: the server cuts *before* a user
 * message, so it is the next user turn's ref — `null` past the tail, a copy of
 * the whole chat. `undefined` while that turn has no ref: its run is still
 * going, or held its refs frame back (they arrive with the next history load).
 * @param {HTMLElement} turn
 * @returns {string | null | undefined}
 */
function branchCut(turn) {
  let next = turn.nextElementSibling;
  while (next && !next.matches('.turn.user')) next = next.nextElementSibling;
  if (!next) return null;
  return /** @type {HTMLElement} */ (next).dataset.ref || undefined;
}

/**
 * "Branch in new chat" on a settled reply whose cut is known (`branchCut`):
 * the new chat keeps everything up to and including it. Redrawn when a user
 * turn lands after the reply (appendUserTurn) and when refs arrive (stampRefs).
 * @param {HTMLElement} turn
 * @param {Element} footer
 */
function renderBranchButton(turn, footer) {
  footer.querySelector(':scope > .btn-branch')?.remove();
  if (!turn.matches('.turn.assistant:not(.streaming)') || branchCut(turn) === undefined) {
    return;
  }
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn-branch';
  btn.title = t('chat.branch');
  btn.setAttribute('aria-label', t('chat.branch'));
  btn.innerHTML = icon('git-branch', { size: 13 });
  btn.addEventListener('click', async () => {
    const source = store.sessionId;
    const cut = branchCut(turn); // asked again: the turns may have moved on
    if (!source || cut === undefined) return;
    btn.disabled = true;
    await forkChat(source, cut);
    btn.disabled = false;
  });
  const anchor = footer.querySelector(':scope > .btn-copy');
  if (anchor) anchor.after(btn);
  else footer.append(btn);
}

function renderAllMessageActions(from = null) {
  const transcriptEl = document.getElementById('transcript');
  const turns = transcriptEl?.querySelectorAll(':scope > .turn') ?? [];
  let on = !from;
  for (const turn of turns) {
    on ||= turn === from;
    if (on) renderMessageActions(turn.querySelector('.bubble'));
  }
}

watchMessageActions(() => renderAllMessageActions());

// A run named its user turns (the `refs` frame, just before `done`): they are
// the last confirmed ones on screen. Stamp them, then redraw the actions of
// every message from the first — a reply's ref is its user turn's.
function stampRefs(refs) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl || !refs?.length) return;
  const users = [...transcriptEl.querySelectorAll(':scope > .turn.user:not(.queued)')]
    .slice(-refs.length);
  const skip = refs.length - users.length;
  // A turn the snapshot rendered is in the history "Load earlier" redraws
  // from too, at its absolute ordinal (live ones aren't): name it there.
  const cached = _historyEntries.filter((e) => e.role === 'user');
  users.forEach((node, i) => {
    const turn = /** @type {HTMLElement} */ (node);
    turn.dataset.ref = refs[skip + i];
    const entry = cached[Number(turn.dataset.userTurn)];
    if (entry) entry.ref = refs[skip + i];
  });
  // From the reply above the first: its branch cut is that turn's ref.
  if (users.length) renderAllMessageActions(previousTurn(users[0]) ?? users[0]);
}

// ---- Mid-run injection: queued user turns awaiting confirmation ----------
let _queuedTurns = [];   // FIFO of muted user-turn nodes (one per pending inject)
const _drained = new WeakSet(); // the ones a run's user_injected confirmed
let _pendingResend = []; // { message, node } pairs that raced a run's end (see runStream)

// True when an assistant turn carries no rendered content yet (just opened, or
// only an empty footer). Avoids stranding an empty bubble between two messages
// injected at the same turn boundary.
function assistantTurnIsEmpty(node) {
  const bubble = node?.querySelector('.bubble');
  if (!bubble) return true;
  for (const child of bubble.children) {
    if (!child.classList.contains('turn-footer') && !child.classList.contains('awaiting')) {
      return false;
    }
  }
  return true;
}

// Finalize the current (streaming) assistant turn: stop its spinner, stamp it,
// add a copy button, and reset the live-render pointers so the next turn opens
// clean. Shared by the run-end paths and the injection bubble rotation.
function finalizeCurrentAssistantTurn() {
  document.getElementById('awaiting-model')?.remove(); // re-placed in the next bubble
  finalizeReasoning();
  const node = store.turnNode;
  if (node) {
    if (assistantTurnIsEmpty(node)) {
      // e.g. an attach opened a tail bubble the run never wrote into —
      // don't leave an empty grey bubble in the transcript.
      node.remove();
    } else {
      node.classList.remove('streaming');
      // The server's finish (the `timing` frame) outranks the teardown's clock.
      if (!node.dataset.finished) setTurnTimestamp(node);
      sealActivity(store.bubble);
      addMessageActions(store.bubble);
    }
  }
  store.turnNode = null;
  store.bubble = null;
  store.body = null;
  store.rawText = '';
  store.toolNodes.clear();
  store.reasoningNode = null;
  store.reasoningText = '';
  store.reasoningStart = 0;
  store.reasoningEnd = 0;
}

// Promote a muted "queued" user bubble to a normal one once the run consumes it.
function confirmQueuedTurn(node) {
  if (!node) return;
  node.classList.remove('queued');
  node.querySelector('.withdraw-btn')?.remove();
  delete node.dataset.injectId;
  // Confirmation order matches the server's drain order — number it now.
  node.dataset.userTurn = String(_userTurnCount++);
  addMessageActions(node.querySelector('.bubble'));
  // No edit button: this runs mid-run, and the pencil belongs to the tail turn
  // once the run settles (updateEditButton, called from exitStreamingUI).
}

// Add a cancel affordance to a queued bubble once its server token is known, so
// the user can withdraw the message before the run drains it.
function addWithdrawButton(node, injectId) {
  const bubble = node?.querySelector('.bubble');
  if (!bubble) return;
  node.dataset.injectId = String(injectId);
  if (bubble.querySelector('.withdraw-btn')) return;
  const btn = document.createElement('button');
  btn.className = 'withdraw-btn';
  btn.type = 'button';
  btn.title = t('composer.queuedCancel');
  btn.setAttribute('aria-label', t('composer.queuedCancel'));
  btn.innerHTML = icon('x', { size: 13 });
  btn.addEventListener('click', () => withdrawQueued(node));
  bubble.appendChild(btn);
}

// Withdraw a queued message: drop its bubble and ask the server to remove it
// from the run's mailbox (best-effort — it may already have been consumed).
async function withdrawQueued(node) {
  const i = _queuedTurns.indexOf(node);
  if (i >= 0) _queuedTurns.splice(i, 1);
  const id = node.dataset.injectId;
  node.remove();
  if (id) {
    try {
      await api.uninject({ session_id: store.sessionId, id: Number(id) });
    } catch {
      /* best-effort */
    }
  }
}

// Un-mute every still-queued bubble (e.g. an errored run dropped them) so they
// read as sent rather than stuck pending.
function flushQueuedTurns() {
  for (const node of _queuedTurns) confirmQueuedTurn(node);
  _queuedTurns = [];
}

// Re-inject messages that raced the previous run's end into the now-live run —
// as separate turns, in order. Joining them into one message would fuse
// distinct turns server-side while the transcript shows several, skewing the
// user-turn ordinals rewind depends on.
async function drainPendingResend() {
  const sid = store.sessionId;
  if (!_pendingResend.length || !sid) return;
  const items = _pendingResend.splice(0);
  for (const it of items) {
    // A snapshot re-render may have wiped the muted bubble — recreate it.
    const bubble = it.node?.isConnected ? it.node : appendUserTurn(it.message, { queued: true });
    const { node, outcome } = await steer(it.message, bubble);
    // Still no live run — the next finally retries.
    if (outcome === 'refused') _pendingResend.push({ message: it.message, node });
    else if (outcome === 'unknown') parkUnsure(sid, node, it.message, _shownRunId);
  }
}

// Seed a fresh run with the oldest raced message; the rest re-enter the queue
// once that run's stream opens (drainPendingResend). Returns true when a
// resend was started.
function resumePendingResend() {
  const next = _pendingResend.shift();
  if (!next) return false;
  if (next.node?.isConnected) {
    confirmQueuedTurn(next.node);
  } else {
    appendUserTurn(next.message);
  }
  runStream(next.message);
  return true;
}

// ---- History rendering --------------------------------------------------
// The transcript renders a bounded tail of the history; earlier chunks load
// on demand. Full replay of a months-long chat froze the tab on open — the
// DOM cost, not the fetch, is the bottleneck (entries are already in hand).
const HISTORY_PAGE = 150; // entries per window step
let _historyEntries = [];
let _historyStart = 0; // index of the first rendered entry

// Snap a window start onto a user turn so a tool result never renders
// without the call (and turn pairs stay intact).
function alignHistoryStart(idx) {
  if (idx <= 0) return 0;
  for (let i = idx; i < _historyEntries.length; i++) {
    if (_historyEntries[i].role === 'user') return i;
  }
  return idx;
}

/**
 * Replace the transcript with a session's history entries (paged internally).
 * @param {any[]} entries Transcript entries from the session API.
 */
export function renderHistory(entries) {
  _historyEntries = Array.isArray(entries) ? entries : [];
  _historyStart = alignHistoryStart(_historyEntries.length - HISTORY_PAGE);
  renderHistoryWindow({ stickBottom: true });
}

/** The "load earlier" button waits out a run: its redraw would wipe the live answer. */
function syncLoadEarlier() {
  const btn = /** @type {HTMLButtonElement | null} */ (
    document.querySelector('#transcript .load-earlier')
  );
  if (!btn) return;
  btn.disabled = !!store.streaming;
  btn.title = store.streaming ? t('chat.loadEarlierBusy') : '';
}

// Bumped whenever the transcript gains turns or is redrawn (a run starts, a
// history window renders), so a fetch made for the view before can tell it's
// stale — even if a whole run began and ended while it was out.
let _viewVersion = 0;

// "Load earlier" redraws the whole window from _historyEntries, so it first
// refetches them: turns streamed live since the history loaded aren't in the
// snapshot and would vanish. History only grows at the tail, so the window's
// start index holds across the refetch. A redraw mid-stream would wipe the
// live answer, so the button waits out a run (syncLoadEarlier).
async function loadEarlierHistory() {
  const el = document.getElementById('transcript');
  const sessionId = store.sessionId;
  if (!el || !sessionId || store.streaming) return;
  const epoch = store.chatEpoch;
  const version = _viewVersion;
  let data;
  try {
    data = await api.getSession(sessionId);
  } catch {
    toast(t('toast.loadChatsFailed'), { type: 'error' });
    return;
  }
  // Superseded: another chat, or this one changed (a run, a redraw) meanwhile —
  // the fetched history may predate what's on screen now.
  if (store.chatEpoch !== epoch || store.sessionId !== sessionId || _viewVersion !== version) {
    return;
  }
  _historyEntries = Array.isArray(data.entries) ? data.entries : [];
  const prevHeight = el.scrollHeight;
  const prevTop = el.scrollTop;
  _historyStart = alignHistoryStart(_historyStart - HISTORY_PAGE);
  renderHistoryWindow({ stickBottom: false });
  // Keep the viewport anchored on the content it was showing.
  requestAnimationFrame(() => {
    el.scrollTop = el.scrollHeight - prevHeight + prevTop;
    _lastScrollTop = el.scrollTop;
  });
}

function renderHistoryWindow({ stickBottom }) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return;
  _viewVersion++;
  const entries = _historyEntries;
  // Swapping the transcript collapses scrollHeight and snaps scrollTop to 0,
  // firing a 'scroll' event the handler would misread as the user scrolling up
  // — which disables sticky-bottom. Guard the swap exactly like scrollDown()
  // does: the reset's (coalesced) scroll event fires before the rAF below
  // releases the flag, so it's ignored. Without this, switching back to a
  // still-streaming chat rendered its snapshot and then never re-pinned to the
  // live tail.
  _programmaticScroll = true;
  transcriptEl.innerHTML = '';
  if (stickBottom) _resumeAutoScroll();
  resetChatView();
  store.bubble = null;
  store.body = null;
  store.rawText = '';
  store.toolNodes.clear();

  // Results are looked up from the FULL history — a window boundary must not
  // orphan a call from its result.
  const pendingResults = new Map();
  for (const it of entries) {
    // History entries are MessageOut (role + tool_call_id), with no `type`
    // field — gating on `it.type` here left every result unmatched and hidden.
    if (it.role === 'tool' && it.tool_call_id)
      pendingResults.set(it.tool_call_id, {
        text: contentText(it.content),
        isError: !!it.is_error,
        images: it.images,
      });
  }

  // Absolute user-turn numbering spans the FULL history — the rendered
  // window may start mid-transcript.
  _userTurnCount = entries.filter((e) => e.role === 'user').length;
  let userIdx = entries
    .slice(0, _historyStart)
    .filter((e) => e.role === 'user').length;

  if (_historyStart > 0) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'btn btn-ghost btn-sm load-earlier';
    more.textContent = t('chat.loadEarlier', { n: _historyStart });
    more.addEventListener('click', loadEarlierHistory);
    transcriptEl.appendChild(more);
    syncLoadEarlier(); // a re-attach snapshot draws it mid-run
  }

  let currentBubble = null;
  for (const it of entries.slice(_historyStart)) {
    if (it.role === 'user') {
      currentBubble = null;
      if (it.timestamp) {
        maybeDateSeparator(transcriptEl, it.timestamp, { anchorFirst: true });
      }
      const turn = makeTurn('user', it.timestamp);
      turn.dataset.userTurn = String(userIdx++);
      if (it.ref) turn.dataset.ref = it.ref;
      const bubble = turn.querySelector('.bubble');
      // A turn's attachments survive in stored history only as the note the
      // server appends to the text (attachments.py `_attachment_note`); recover
      // the previews from it and show the clean message — matching live send.
      const { text: bodyText, attachments } = splitAttachmentNote(contentText(it.content));
      if (bodyText) {
        const body = document.createElement('div');
        body.className = 'body';
        body.textContent = bodyText;
        appendBubbleContent(bubble, body);
      }
      if (attachments.length) appendBubbleContent(bubble, makeAttachmentsBlock(attachments));
      ensureFooter(bubble);
      transcriptEl.appendChild(turn); // first: its actions look for the user turn above
      addMessageActions(bubble);
    } else if (it.role === 'assistant') {
      if (!currentBubble) {
        if (it.timestamp) {
          maybeDateSeparator(transcriptEl, it.timestamp, { anchorFirst: true });
        }
        const result = startAssistantTurn(it.timestamp);
        currentBubble = result.bubble;
        // Its question, by absolute ordinal: the window may start below it.
        if (userIdx > 0) result.node.dataset.question = String(userIdx - 1);
      }
      const text = contentText(it.content);
      if (it.reasoning) {
        const details = document.createElement('details');
        details.className = 'reasoning done';
        const summary = document.createElement('summary');
        // History keeps no reasoning duration, hence not 'chat.thought'.
        summary.innerHTML = `<span class="reasoning-icon">💭</span><span class="reasoning-label">${t('chat.thoughtDone')}</span>`;
        details.appendChild(summary);
        const rc = document.createElement('div');
        rc.className = 'reasoning-content';
        rc.textContent = it.reasoning;
        details.appendChild(rc);
        appendBubbleContent(currentBubble, details);
      }
      if (text) {
        const body = document.createElement('div');
        body.className = 'body';
        body.dataset.raw = text; // store raw markdown for copy
        renderMarkdownInto(body, text, { agent: store.agent });
        appendBubbleContent(currentBubble, body);
        highlightCode(body);
        renderMermaid(body);
      }
      if (it.tool_calls) {
        for (const call of it.tool_calls) {
          // Replayed history counts too: "touched" means files THIS chat
          // produced, whether live or reloaded.
          emitWorkspaceTouch(call.name, toolPath(call.arguments));
          const todos = parseTodos(call.arguments);
          if (todos) {
            upsertTodoCard(todos); // render/update the session's checklist panel
            continue;
          }
          const node = buildToolNode(call);
          const result = pendingResults.get(call.id);
          // Attached first, so a UI component renders into a laid-out card.
          appendBubbleContent(currentBubble, node);
          // Same renderer as the live path: highlighting, error styling, and
          // the copy/expand actions all match. A call with no result yet stays
          // running (its empty <pre> is hidden by CSS).
          if (result) setToolResult(node, result.text, result.isError, result.images);
          else refreshTool(node);
          // A result-less ask_human call is still parked server-side: rebuild
          // its interactive card so a reloaded or re-attached tab can answer
          // (the live path can't — this render replaced the transcript).
          if (call.name === QUESTION_TOOL && !result) {
            const question = buildQuestionCard(call);
            if (question) {
              appendBubbleContent(currentBubble, question);
              setToolStatus(node, 'awaiting'); // as the live path marks it
            }
          }
        }
      }
      addMessageActions(currentBubble);
      // A reply reads as of its last message — a timed run's is its finish,
      // as the live view stamps it at `done`.
      if (it.timestamp) setTurnTimestamp(currentBubble.closest('.turn'), it.timestamp);
      if (it.duration != null) stampDuration(currentBubble, it.duration);
    } else if (it.role === 'context_compacted') {
      // Persisted run-boundary notice — render into the run's bubble (matching
      // the live placement), or the transcript if the run had no assistant turn.
      appendContextCompacted(currentBubble || transcriptEl, it.compaction);
    }
  }

  // Remove streaming markers
  transcriptEl.querySelectorAll('.turn.streaming').forEach(n => n.classList.remove('streaming'));
  renderAllMessageActions(); // the replies were still marked streaming above
  store.bubble = null;
  store.body = null;
  store.rawText = '';
  if (stickBottom) {
    // Land at the bottom now (content is static at this point) and record it as
    // _lastScrollTop so the swap's async scroll event reads as "no movement";
    // then release the guard next frame. Live deltas re-pin via scrollDown().
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    _lastScrollTop = transcriptEl.scrollTop;
  }
  requestAnimationFrame(() => { _programmaticScroll = false; });
  updateRegenButton();
  updateEditButton();
}

// ---- Scroll ------------------------------------------------------------
let _stickToBottom = true;
let _programmaticScroll = false;
let _scrollFrame = null;
let _userScrollPauseUntil = 0;
let _lastScrollTop = 0;
const scrollBtn = document.getElementById('scroll-bottom');
function _isAtBottom() {
  const el = document.getElementById('transcript');
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight < STICKY_SCROLL_PX;
}
function updateScrollButton() {
  scrollBtn?.classList.toggle('visible', !_isAtBottom());
}
function _isUserScrollPaused() {
  return Date.now() < _userScrollPauseUntil;
}
function _pauseAutoScroll() {
  if (_scrollFrame) {
    cancelAnimationFrame(_scrollFrame);
    _scrollFrame = null;
  }
  _stickToBottom = false;
  _userScrollPauseUntil = Date.now() + USER_SCROLL_PAUSE_MS;
}
function _resumeAutoScroll() {
  _userScrollPauseUntil = 0;
  _stickToBottom = true;
  const el = document.getElementById('transcript');
  if (el) _lastScrollTop = el.scrollTop;
}
function scrollDown() {
  if (!_stickToBottom || _isUserScrollPaused() || _scrollFrame) return;
  _scrollFrame = requestAnimationFrame(() => {
    _scrollFrame = null;
    if (!_stickToBottom || _isUserScrollPaused()) return;
    const el = document.getElementById('transcript');
    if (!el) return;
    _programmaticScroll = true;
    el.scrollTop = el.scrollHeight;
    requestAnimationFrame(() => {
      _programmaticScroll = false;
      _lastScrollTop = el.scrollTop;
      _stickToBottom = !_isUserScrollPaused() && _isAtBottom();
      updateScrollButton();
    });
  });
}
const transcriptEl = document.getElementById('transcript');
transcriptEl?.addEventListener('wheel', (e) => {
  if (e.deltaY < 0) {
    _pauseAutoScroll();
  } else {
    requestAnimationFrame(() => {
      if (_isAtBottom()) _resumeAutoScroll();
    });
  }
}, { passive: true });
transcriptEl?.addEventListener('scroll', () => {
  if (_programmaticScroll) return;
  const current = transcriptEl.scrollTop;
  const movedUp = current < _lastScrollTop;
  const movedDown = current > _lastScrollTop;
  _lastScrollTop = current;

  if (movedUp) {
    _pauseAutoScroll();
  } else if (_isAtBottom() && (movedDown || !_isUserScrollPaused())) {
    _resumeAutoScroll();
  } else {
    _stickToBottom = false;
  }
  updateScrollButton();
}, { passive: true });

scrollBtn?.addEventListener('click', () => {
  _resumeAutoScroll();
  scrollDown();
});

/** The first-run setup hero: shown instead of the welcome view until the
 * server has a model to chat with (CLI default agent only). */
function renderSetupHero(transcript) {
  const hero = document.createElement('div');
  hero.className = 'empty-state setup-hero';
  hero.id = 'empty-state';
  const glyph = document.createElement('div');
  glyph.className = 'setup-hero-glyph';
  glyph.textContent = '◈';
  const h2 = document.createElement('h2');
  h2.textContent = t('cfg.heroTitle');
  const p = document.createElement('p');
  p.textContent = t('cfg.heroBody');
  const cta = document.createElement('button');
  cta.type = 'button';
  cta.className = 'btn btn-primary setup-hero-cta';
  cta.textContent = t('cfg.heroCta');
  cta.addEventListener('click', () => store.emit('open-model-settings'));
  const alt = document.createElement('div');
  alt.className = 'setup-hero-alt';
  alt.textContent = t('cfg.heroAlt');
  hero.append(glyph, h2, p, cta, alt);
  transcript.replaceChildren(hero);
}

/** Render the empty-state (welcome) view — title, description, example prompts — into the transcript. */
export function renderEmptyState() {
  const transcript = document.getElementById('transcript');
  if (!transcript) return;
  if (store.canConfigureModels && !store.configured) {
    renderSetupHero(transcript);
    return;
  }
  // The server renders the welcome (its `welcome` block, overrides included)
  // into the page once and into this template; restore it from there.
  const empty = document.createElement('div');
  empty.className = 'empty-state';
  empty.id = 'empty-state';
  empty.dataset.part = 'welcome';
  const tmpl = /** @type {HTMLTemplateElement | null} */ (document.getElementById('tmpl-welcome'));
  if (tmpl) {
    const content = /** @type {DocumentFragment} */ (tmpl.content.cloneNode(true));
    applyStaticI18n(content);
    empty.appendChild(content);
  }
  transcript.replaceChildren(empty);
}

// ---- SSE ---------------------------------------------------------------
// True while the most recent SSE event was `error`. If the stream then ends
// (no `done`), the failure was terminal — offer a Retry; a mid-run tool error
// is followed by more events, which reset this.
let _lastEventWasError = false;

// True once this stream produced at least one `done` (an auto-chaining run
// emits one per leg). Read in the stream's finally to tell "the run answered"
// from "cancelled or died", which is what gates the follow-up chips.
let _sawDone = false;

// This stream's latest event: a run that answered ends on `done`, while an
// auto-chained one that broke off after a leg's `done` ends on something else.
let _lastEvent = '';

// This stream's last assistant text, for the follow-up cache signature. It
// can't be read off store.rawText in the finally: `message_completed` clears
// that as each model turn lands, long before `done` — so the finally always
// saw '' and every cache write was silently skipped. Taken from the event
// payload instead, which is the same joined turn text the server persists
// (sse.py `_entries_to_dict` / transcript.py `entries_to_messages`), so this
// signature matches the one `tailSig` recomputes from reloaded history.
let _lastReply = '';

// ---- Waiting on the model ------------------------------------------------
// From a send (and each model call's turn_started) until the model's first
// sign of life, the bubble's tail breathes three dots — with a line while the
// provider is being retried. Re-placed at the tail after every event, so a
// notice landing meanwhile (a compaction) never buries it.
/** @type {{ retry: { attempt: number, max_attempts: number, message?: string } | null } | null} */
let _awaiting = null;
const MODEL_RESPONDED = new Set([
  'text_delta', 'reasoning_delta', 'tool_call', 'approval_required',
  'message_completed', 'error', 'done',
]);

function syncAwaiting() {
  let el = document.getElementById('awaiting-model');
  if (!_awaiting || !store.bubble) {
    el?.remove();
    return;
  }
  if (!el) {
    el = document.createElement('div');
    el.id = 'awaiting-model';
    el.className = 'awaiting';
    const dots = document.createElement('span');
    dots.className = 'awaiting-dots';
    dots.append(...[0, 1, 2].map(() => document.createElement('i')));
    const text = document.createElement('span');
    text.className = 'awaiting-text';
    el.append(dots, text);
  }
  const retry = _awaiting.retry;
  /** @type {HTMLElement} */ (el.querySelector('.awaiting-text')).textContent = retry
    ? t('chat.retrying', { n: retry.attempt + 1, max: retry.max_attempts })
    : '';
  el.title = retry?.message ?? '';
  // Last, or just above the footer an adopted (reconnected) bubble carries.
  const footer = store.bubble.querySelector(':scope > .turn-footer');
  if (el.parentNode !== store.bubble || el.nextElementSibling !== footer) {
    store.bubble.insertBefore(el, footer);
  }
}

/** @param {{ event: string, data: any }} ev */
async function handleEvent(ev) {
  if (MODEL_RESPONDED.has(ev.event)) _awaiting = null;
  await applyEvent(ev);
  syncAwaiting();
}

/** @param {{ event: string, data: any }} ev */
async function applyEvent({ event, data }) {
  _lastEventWasError = event === 'error';
  _lastEvent = event;
  switch (event) {
    case 'session': {
      const known = store.sessions.some((s) => s.id === data.session_id);
      store.sessionId = data.session_id;
      if (_shownRun) _shownRun.sessionId = data.session_id;
      store.syncURL(data.session_id);
      // A brand-new session gets a server-generated title shortly after the
      // first turn; flag it so the stream's end polls for it (see pollForTitle).
      store.titlePending = !known;
      // Surface it in the sidebar right away — with its provisional title —
      // instead of waiting for the run to finish.
      if (!known) loadSessions();
      // The run this view follows, by the id its lifecycle events carry: what
      // a message queued now waits on.
      _shownRunId = data.run_id ?? null;
      updateSendEnabled(); // a message can queue behind it now
      _onAdmit?.(data.run_id);
      _onAdmit = null;
      // The run is registered now: a stop clicked while it was being admitted
      // can reach it.
      _admitting = false;
      if (_stopOnceAdmitted) {
        _stopOnceAdmitted = false;
        cancelStream();
      }
      break;
    }

    case 'snapshot':
      // Authoritative re-attach snapshot: replace the transcript with the run's
      // history-so-far, then keep writing into its tail assistant turn so the
      // live continuation joins the run it belongs to (see
      // resumeTailAssistantTurn). Only a run with no assistant turn yet opens
      // a bubble of its own.
      renderHistory(data.entries || []);
      if (!resumeTailAssistantTurn()) startAssistantTurn();
      break;

    case 'text_delta':
      finalizeReasoning();
      ensureBody();
      store.rawText += data.delta;
      scheduleRender();
      break;

    case 'reasoning_delta': {
      ensureReasoning();
      if (!store.reasoningStart) store.reasoningStart = Date.now();
      store.reasoningEnd = Date.now();
      store.reasoningText += data.delta;
      const content = store.reasoningNode?.querySelector('.reasoning-content');
      // Same selection guard as flushRender — the next delta catches up.
      if (content && !selectionInside(content)) {
        content.textContent = store.reasoningText;
      }
      scrollDown();
      break;
    }

    case 'output_discarded':
      // A transient mid-stream error discarded this turn's partial output; a
      // fresh stream that replaces it follows. Drop what's on screen so the
      // retry's text doesn't append to the abandoned attempt. Only text and
      // reasoning can be live here — tool_call events fire after the model
      // stream — and store.reasoningNode is null between turns, so it only
      // ever refers to the current interrupted turn.
      cancelRender();
      if (store.body) { store.body.remove(); store.body = null; }
      store.rawText = '';
      if (store.reasoningNode) { removeStep(store.reasoningNode); store.reasoningNode = null; }
      store.reasoningText = '';
      store.reasoningStart = 0;
      store.reasoningEnd = 0;
      // The replacing attempt is under way: wait on it (keeping a retry line
      // that's showing; a reconnect's replayed deltas cleared any other).
      if (!_awaiting) _awaiting = { retry: null };
      break;

    case 'message_completed':
      cancelRender();
      if (store.body && store.rawText) flushRender(true);
      store.body = null;
      store.rawText = '';
      // Remember the answer before it's gone — a tool-call-only turn carries no
      // text, so keep the last turn that actually said something.
      if (data.message?.content) _lastReply = data.message.content;
      if (!store.reasoningNode && data.message?.reasoning && store.bubble) {
        ensureReasoning();
        store.reasoningText = data.message.reasoning;
        if (store.reasoningNode)
          store.reasoningNode.querySelector('.reasoning-content').textContent = store.reasoningText;
        store.reasoningEnd = Date.now();
      }
      finalizeReasoning();
      store.reasoningNode = null;
      store.reasoningText = '';
      store.reasoningStart = 0;
      store.reasoningEnd = 0;
      break;

    case 'user_injected': {
      // A queued message was consumed at this turn's start. Close out the
      // assistant work that preceded it (once per batch — only when the current
      // bubble has content) so the response opens a fresh bubble below the
      // injected user turn(s).
      if (store.turnNode && !assistantTurnIsEmpty(store.turnNode)) {
        finalizeCurrentAssistantTurn();
        startAssistantTurn();
      }
      const queued = _queuedTurns.shift();
      if (queued) {
        _drained.add(queued);
        confirmQueuedTurn(queued);
      } else {
        // Injected elsewhere (e.g. another tab): render above the fresh bubble.
        appendUserTurn(data.content, { before: store.turnNode });
      }
      scrollDown();
      break;
    }

    case 'tool_call':
      {
        const todos = parseTodos(data.arguments);
        if (todoNames.has(data.name) || todos) {
          if (todos) {
            todoNames.add(data.name);
            upsertTodoCard(todos);
          }
          break; // rendered as a todo panel instead
        }
      }
      finalizeReasoning();
      appendTool(data);
      // ask_human parks the run on the user: draw the interactive card too.
      if (data.name === QUESTION_TOOL) appendQuestion(data);
      break;

    case 'tool_result':
      // A finished shell command — or a background process observed via a
      // poll/kill — may have created/edited files we can't see individually;
      // let the Files panel mark its listing as maybe stale.
      if (STALE_TOOLS.has(data.name)) store.emit('workspace-maybe-stale');
      // The call resolved out-of-band (another tab answered, timeout,
      // cancel): freeze the interactive card to match.
      if (data.name === QUESTION_TOOL) {
        settleQuestion(
          findQuestionNode(data.id),
          data.is_error ? null : String(data.result ?? ''),
        );
      }
      updateToolResult(data.id, data.result, data.is_error, data.images);
      // A write counts as "touched" once it landed — not at the call, which
      // a denied approval or a failing tool may never turn into a file.
      if (!data.is_error) {
        emitWorkspaceTouch(data.name, store.toolNodes.get(data.id)?.dataset.toolPath);
      }
      break;

    case 'todo':
      finalizeReasoning();
      if (data.name) todoNames.add(data.name);
      removeToolNode(data.call_id); // drop the bubble if it slipped through
      upsertTodoCard(data.todos || []);
      break;

    case 'approval_required':
      appendApproval(data);
      break;

    case 'handoff':
      appendHandoff(data.from, data.to);
      break;

    case 'retrying':
      _awaiting = { retry: data };
      announce(t('chat.retrying', { n: data.attempt + 1, max: data.max_attempts }));
      break;

    case 'turn_started':
      _awaiting = { retry: null };
      break;

    case 'context_compacted':
      appendContextCompacted(store.bubble, data);
      break;

    case 'error': {
      // `message` can arrive empty (older servers didn't fall back for
      // message-less exceptions); the class name in `type` beats a bare ⚠️.
      const message = data.message || data.type;
      _lastErrorCode = data.code ?? '';
      appendErrorNotice(data.hint ? `${message} — ${data.hint}` : message, data.code);
      break;
    }

    case 'refs':
      stampRefs(data?.refs);
      break;

    case 'timing':
      // The finished leg's timing as its segment recorded it: the answer reads
      // as on replay — as of its finish, with how long its run took.
      const turn = /** @type {HTMLElement | null | undefined} */ (store.bubble?.closest('.turn'));
      setTurnTimestamp(turn, data.finished_at);
      if (turn) turn.dataset.finished = '1';
      stampDuration(store.bubble, data.duration);
      break;

    case 'done': {
      _sawDone = true;
      if (_shownRun) _shownRun.usage = data?.usage ?? null;
      _dropRecoveries = 0; // the connection proved healthy — re-arm recovery
      updateContextMeter(data?.usage);
      // Stamp the run's token spend into the turn footer — the data is
      // already on the wire; hovering shows the input/output split. A zero
      // total means the provider reported no usage, not a free run.
      const tokens = data?.usage?.total_tokens ? formatTokens(data.usage.total_tokens) : null;
      if (tokens && store.bubble) {
        const footer = ensureFooter(store.bubble);
        if (footer && !footer.querySelector('.usage')) {
          const span = document.createElement('span');
          span.className = 'usage';
          span.title = `${formatTokens(data.usage.input_tokens) ?? '?'} in · ${formatTokens(data.usage.output_tokens) ?? '?'} out`;
          span.textContent = `${tokens} tok`;
          footer.appendChild(span);
        }
      }
      break;
    }
  }
}

// ---- Title polling -----------------------------------------------------
// A new chat's title is produced by a background task on the server after the
// first turn. Poll the session list with bounded back-off until the
// provisional title is replaced, then stop. Only one poller runs at a time —
// previous unbounded pollers could pile up and churn the sidebar forever.
const _TITLE_POLL_BACKOFF_MS = [600, 800, 1500, 3000, 5000, 8000, 12000];
let _titlePollTimer = null;

function stopTitlePolling() {
  clearTimeout(_titlePollTimer);
  _titlePollTimer = null;
}

async function pollForTitle(sessionId) {
  stopTitlePolling();
  await loadSessions(); // surface the provisional row right away
  if (store.sessionId !== sessionId) return; // user moved on
  // Baseline to detect the replacement against. The sidebar row is the free
  // source; with a search filter hiding the row, ask the server directly.
  const provisional =
    store.sessions.find((s) => s.id === sessionId)?.title ??
    (await api.getSession(sessionId).catch(() => null))?.title ??
    null;
  let attempt = 0;

  // Keep the timer callback synchronous and swallow rejections so a failed
  // poll can never surface as an unhandled promise rejection.
  const schedule = (ms) => {
    _titlePollTimer = setTimeout(() => void tick().catch(() => {}), ms);
  };

  async function tick() {
    _titlePollTimer = null;
    if (store.sessionId !== sessionId) return;
    // Poll the session itself, not the list: immune to the sidebar filter and
    // cheap for a first-turn chat. A fetch blip counts as "not yet" so the
    // remaining back-off steps still retry.
    const current = (await api.getSession(sessionId).catch(() => null))?.title ?? null;
    if (current && current !== provisional) {
      updateSessionTitle(sessionId, current); // row (if visible) + topbar
    } else if (attempt < _TITLE_POLL_BACKOFF_MS.length) {
      schedule(_TITLE_POLL_BACKOFF_MS[attempt++]);
    }
  }

  schedule(_TITLE_POLL_BACKOFF_MS[attempt++]);
}

// ---- Follow-up chips ---------------------------------------------------
// Questions the user might ask next, offered once a run settles. Fetched from
// the stream's finally rather than on the `done` event: an auto-chaining run
// emits one `done` per leg inside a single stream, and we want one suggestion
// call per stream, not per leg.
//
// The chips live at the tail of the transcript, outside any turn, so sending
// anything simply removes them (see runStream) and a history re-render drops
// them with the rest of the DOM. A successful fetch also caches them per
// session (followup-cache.js) so a refresh or a switch away can repaint the
// same chips without spending another call — see maybeRestoreFollowups.
let _followupController = null;

// Longest reply prefix folded into the cache signature. The server derives its
// suggestions from only the first ~1200 chars of the reply (followups.py
// `_MAX_REPLY`), so a wider window could never change the outcome — and a
// bounded hash keeps the cost fixed no matter how large the reply is.
const _SIG_MAX = 4096;

// FNV-1a over a bounded reply prefix → a short, stable signature string.
function hashReply(text) {
  let h = 0x811c9dc5;
  const n = Math.min(text.length, _SIG_MAX);
  for (let i = 0; i < n; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(36);
}

/**
 * Signature of a transcript's final exchange — what the follow-up cache keys
 * on. Mirrors the server's `_last_exchange`: the reply is the last non-empty
 * assistant message. Returns null when the tail isn't a settled reply — a
 * trailing unanswered user turn, or no reply at all — so cached chips are never
 * restored where the server itself would have produced none.
 * @param {any[]} entries
 * @returns {string | null}
 */
function tailSig(entries) {
  if (!Array.isArray(entries)) return null;
  for (let i = entries.length - 1; i >= 0; i--) {
    const text = contentText(entries[i].content).trim();
    if (!text) continue; // tool traffic / empty turns carry no exchange
    if (entries[i].role === 'assistant') return hashReply(text);
    if (entries[i].role === 'user') return null; // an unanswered turn trails
  }
  return null;
}

/**
 * Repaint cached follow-up chips for the current session if they still match
 * its tail. Silent on every miss (no cache, stale signature, or feature off),
 * which shows nothing — exactly the pre-cache behaviour.
 * @param {any[]} entries The session's transcript, as just loaded.
 */
function maybeRestoreFollowups(entries) {
  if (!store.canSuggest || !followupsEnabled()) return;
  const sig = tailSig(entries);
  if (!sig) return;
  const items = readFollowups(store.sessionId, sig);
  if (items) renderFollowups(items);
}

function clearFollowups() {
  _followupController?.abort();
  _followupController = null;
  document.getElementById('followups')?.remove();
}

function renderFollowups(items) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl || !items?.length) return;
  const wrap = document.createElement('div');
  wrap.className = 'followups';
  wrap.id = 'followups';
  // A plain <div> has no implicit role, so an aria-label on it has nothing to
  // attach to and most screen readers drop it. `group` gives the label
  // somewhere to land and announces the chips as one region of controls.
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', t('chat.followups'));
  for (const text of items) {
    const btn = document.createElement('button');
    btn.type = 'button';
    // `.chip` is the shared pill *look*; the behaviour class must NOT be
    // `.empty-example`, whose delegated handler on #transcript fills the
    // composer — a follow-up sends instead, and wearing both fired both.
    btn.className = 'chip followup';
    btn.textContent = text;
    btn.addEventListener('click', () => {
      // Unlike a starter prompt (which only fills the composer), a follow-up
      // sends: one click is the whole point of the affordance.
      document.getElementById('empty-state')?.remove();
      appendUserTurn(text);
      runStream(text); // fire-and-forget; clearFollowups runs at its head
    });
    wrap.appendChild(btn);
  }
  transcriptEl.appendChild(wrap);
  scrollDown();
}

/**
 * Ask the server what the user might want next, render the chips, and cache
 * them against the reply they answer. Silent on every failure — no chips is
 * the neutral outcome.
 * @param {string} sessionId
 * @param {string} [replyText] The reply just produced, for the cache signature.
 */
async function fetchFollowups(sessionId, replyText = '') {
  // Capability (does this server produce them) AND preference (does this user
  // want them). Checked before the fetch, so opting out spends nothing.
  if (!store.canSuggest || !followupsEnabled() || !sessionId) return;
  clearFollowups();
  const epoch = store.chatEpoch;
  const controller = new AbortController();
  _followupController = controller;
  try {
    const data = await api.getFollowups(sessionId, { signal: controller.signal });
    // The suggester thinks for a second or two — long enough for the user to
    // switch chats or start the next turn. Those chips belong to a view that
    // has moved on.
    if (
      store.chatEpoch !== epoch ||
      store.sessionId !== sessionId ||
      store.streaming
    ) {
      return;
    }
    renderFollowups(data?.followups);
    // Persist so a refresh or a trip through another session repaints these
    // without another call. Keyed by the (trimmed) reply, matching what
    // maybeRestoreFollowups recomputes from stored history — a later turn
    // changes the tail, misses, and regenerates.
    const reply = replyText.trim();
    if (reply && data?.followups?.length) {
      writeFollowups(sessionId, hashReply(reply), data.followups);
    }
  } catch {
    /* aborted, offline, or the suggester declined — show nothing */
  } finally {
    if (_followupController === controller) _followupController = null;
  }
}

// switchSession asks us to repaint cached chips once it has settled history in
// place (and isn't about to reconnect to a live run, which produces its own).
store.on('maybe-followups', (entries) => maybeRestoreFollowups(entries || []));

// ---- Streaming ---------------------------------------------------------
const stopBtn = document.getElementById('stop');
const composer = /** @type {HTMLFormElement | null} */ (document.getElementById('composer'));
const promptEl = /** @type {HTMLTextAreaElement | null} */ (document.getElementById('prompt'));
let _streamAbortController = null;
// From a send until its stream's `session` frame, the server is admitting the
// run, and a stop waits for it (see cancelStream).
let _admitting = false;
let _stopOnceAdmitted = false;
// The run the view streams, once its `session` frame names it (null before,
// and after the stream ends): what a message queued now waits on.
/** @type {string | null} */
let _shownRunId = null;
// runStream's ear for that frame: its run was admitted.
/** @type {((runId: string) => void) | null} */
let _onAdmit = null;
// When the run on screen started (epoch seconds; 0: unknown), for the Stop
// button's tooltip: how long it's gone on, there when wanted, never on screen.
let _runStartedAt = 0;

// Below this a run's duration is noise, not worth a footer whisper.
const DURATION_MIN_S = 10;

/** @param {number} sec */
function formatDuration(sec) {
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return t('dur.s', { s });
  if (s < 3600) return t('dur.ms', { m: Math.floor(s / 60), s: s % 60 });
  return t('dur.hm', { h: Math.floor(s / 3600), m: Math.floor((s % 3600) / 60) });
}

/**
 * Whisper how long the run took in its answer's footer, after the timestamp.
 * @param {Element | null} bubble
 * @param {number} sec
 */
function stampDuration(bubble, sec) {
  if (!bubble || sec < DURATION_MIN_S) return;
  const footer = ensureFooter(bubble);
  if (!footer || footer.querySelector(':scope > .duration')) return;
  const span = document.createElement('span');
  span.className = 'duration';
  span.textContent = formatDuration(sec);
  span.title = t('chat.took', { d: span.textContent });
  footer.querySelector(':scope > .timestamp')?.after(span);
}

// ---- Dropped-stream recovery ---------------------------------------------
// A clean SSE EOF without a `done` (and no error event) means the connection
// died mid-run — a proxy idle timeout, a network blip — NOT that the run
// finished. The supervised run usually survives server-side, so re-attach to
// it; if it ended while we were disconnected, reload the transcript so the
// missing tail appears instead of silently showing a truncated reply.
let _dropRecoveries = 0; // consecutive recoveries; `done` resets it
const DROP_RECOVERY_MAX = 3;
const DROP_RECOVERY_DELAY_MS = [400, 1500, 4000];

async function recoverDroppedStream(sessionId) {
  if (_dropRecoveries >= DROP_RECOVERY_MAX) {
    toast(t('chat.reconnectFailed'), { type: 'error' });
    appendReconnectRow(sessionId);
    return;
  }
  const delay = DROP_RECOVERY_DELAY_MS[_dropRecoveries];
  _dropRecoveries += 1;
  if (_dropRecoveries === 1) toast(t('chat.reconnecting'));
  const epoch = store.chatEpoch;
  await new Promise((r) => setTimeout(r, delay));
  if (store.chatEpoch !== epoch || store.streaming || store.sessionId !== sessionId) return;
  const outcome = await runReconnect(sessionId);
  if (outcome === 'attached') return; // its own finally decides what's next
  if (outcome === 'error') {
    return recoverDroppedStream(sessionId); // still unreachable — bounded retry
  }
  // 'norun': the run ended while we were away — fetch the authoritative tail.
  let data;
  try {
    data = await api.getSession(sessionId);
  } catch {
    return recoverDroppedStream(sessionId);
  }
  if (store.chatEpoch !== epoch || store.sessionId !== sessionId || store.streaming) return;
  store.emit('render-history', data.entries || []);
  // The run settled server-side while we were away; its chips (if any) were
  // cached by whichever client saw it finish — repaint them if the tail matches.
  maybeRestoreFollowups(data.entries || []);
  _dropRecoveries = 0;
  resumePendingResend();
}

// The give-up affordance: a manual "Reconnect" at the transcript tail.
function appendReconnectRow(sessionId) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl || document.getElementById('reconnect-row')) return;
  const node = cloneTemplate('tmpl-retry');
  node.id = 'reconnect-row';
  const btn = node.querySelector('.retry-btn');
  btn.textContent = t('chat.reconnect');
  btn.addEventListener('click', () => {
    node.remove();
    _dropRecoveries = 0;
    recoverDroppedStream(sessionId);
  });
  transcriptEl.appendChild(node);
  scrollDown();
}

// ---- Run lifecycle (window.lovia's run-start / run-end) -----------------
// A run starts in this view once its stream is open, and ends when the view
// stops streaming it: answered, failed, stopped by the user, or left — a
// switch or a dropped connection, while the run may go on server-side.
/** @type {{ sessionId: string | null, usage: object | null } | null} */
let _shownRun = null;

function runStarted() {
  _shownRun = { sessionId: store.sessionId, usage: null };
  store.emit('run-start', { sessionId: store.sessionId });
}

/** @param {'done' | 'error' | 'cancelled' | 'detached'} status */
function runEnded(status) {
  if (!_shownRun) return;
  const { sessionId, usage } = _shownRun;
  _shownRun = null;
  // After the ending stream's own cleanup (it runs on synchronously from
  // here): a listener may start the next run, which that cleanup would
  // otherwise clobber (its abort controller, for one).
  queueMicrotask(() => store.emit('run-end', { sessionId, status, usage }));
}

/**
 * How a stream that stayed on screen to its end went. Order matters: an
 * auto-chained run says `done` once per leg, so an earlier leg's answer must
 * not hide a later stop or failure.
 * @param {{ cancelled: boolean, readFailed: boolean, terminal: boolean }} flags
 *   `readFailed`: reading the stream itself failed (the connection broke), as
 *   opposed to the run reporting an error or the page failing on an event.
 * @returns {'done' | 'error' | 'cancelled' | 'detached'}
 */
function runStatus({ cancelled, readFailed, terminal }) {
  if (cancelled) return 'cancelled';
  if (readFailed) return 'detached'; // the run may go on server-side
  if (terminal || _lastEventWasError) return 'error';
  if (_lastEvent === 'done') return 'done';
  return 'detached'; // ended mid-run without a failure: the connection dropped
}

/**
 * The stream's events, marking `flags.readFailed` when reading the body
 * fails — a broken connection — rather than handling an event.
 * @param {Response} res
 * @param {{ readFailed: boolean }} flags
 */
async function* readStream(res, flags) {
  const events = readSSE(res);
  for (;;) {
    let step;
    try {
      step = await events.next();
    } catch (err) {
      if (err?.name !== 'AbortError') flags.readFailed = true;
      throw err;
    }
    if (step.done) return;
    yield step.value;
  }
}

// While a run streams, Stop takes the Send circle's place; Send comes back —
// as a queue control — the moment there's text to queue (updateSendEnabled),
// and pressing Enter queues it for the next turn/run. The placeholder is what
// makes queuing discoverable.
function enterStreamingUI() {
  store.streaming = true;
  _viewVersion++; // the run draws new turns
  syncAttachButton();
  syncLoadEarlier();
  document.getElementById('reconnect-row')?.remove(); // superseded by a live stream
  if (stopBtn) {
    stopBtn.style.display = '';
    stopBtn.title = t('composer.stopTip');
  }
  scrollBtn?.classList.add('live');
  if (promptEl) promptEl.placeholder = t('composer.queuePlaceholder');
  renderQueue(); // its Steer buttons need a live run
  updateSendEnabled();
  // Keep screen readers from re-announcing every 60 ms streaming re-render;
  // they pick the transcript back up once the turn settles.
  document.getElementById('transcript')?.setAttribute('aria-busy', 'true');
  updateRegenButton(); // streaming: no regen affordance until the run settles
  updateEditButton(); // …and no edit either: rewind is refused during a live run
}

/** @param {'done' | 'error' | 'cancelled' | 'detached'} [status] How the run shown went. */
function exitStreamingUI(status = 'detached') {
  store.streaming = false;
  _shownRunId = null;
  _awaiting = null;
  syncAwaiting();
  scrollBtn?.classList.remove('live');
  _runStartedAt = 0;
  syncAttachButton();
  syncLoadEarlier();
  if (stopBtn) stopBtn.style.display = 'none';
  if (promptEl) {
    promptEl.placeholder = t('composer.placeholder');
    promptEl.focus();
  }
  renderQueue(); // no live run: no Steer
  updateSendEnabled();
  document.getElementById('transcript')?.setAttribute('aria-busy', 'false');
  updateRegenButton();
  updateEditButton();
  runEnded(status); // last: listeners see the composer idle again
}

/**
 * How a stream's request went, for a caller that must know: `admitted` once
 * its `session` frame named the run; `refused` when the server turned it
 * away (its error), nothing having started.
 * @typedef {{ admitted: boolean, refused: { code: string, message: string } | null }} StreamOutcome
 */

/**
 * Send `message` (with optional attachments) and stream the assistant's reply
 * into a new turn.
 * @param {string} message
 * @param {any[] | null} [attachments] Prepared attachment descriptors.
 * @param {{ busy?: 'reject', onAdmit?: (runId: string) => void }} [opts]
 *   `busy: 'reject'` — a queued message: never steered into a live run, and
 *   a refusal or a break before admission is reported, not drawn (the
 *   queue's strip says what happened). `onAdmit` hears the run's id.
 * @returns {Promise<StreamOutcome>}
 */
export async function runStream(message, attachments = null, { busy, onAdmit } = {}) {
  const queued = busy === 'reject';
  /** @type {StreamOutcome} */
  const outcome = { admitted: false, refused: null };
  _onAdmit = (runId) => {
    outcome.admitted = true;
    onAdmit?.(runId);
  };
  store.lastMessage = message;
  store.titlePending = false; // set true by the `session` event for new chats
  stopTitlePolling();
  clearFollowups(); // the previous turn's suggestions are answered or abandoned
  _dropRecoveries = 0; // a fresh user action re-arms auto-recovery
  enterStreamingUI();
  startAssistantTurn();
  _awaiting = { retry: null }; // dots from the send, not from the first frame
  syncAwaiting();
  _lastErrorCode = '';
  _runStartedAt = Date.now() / 1000;
  const streamEpoch = store.chatEpoch;
  _lastEventWasError = false;
  _sawDone = false;
  _lastEvent = '';
  _lastReply = '';
  // Set on every path that already surfaced a failure (or a deliberate abort),
  // so the finally's dropped-connection check doesn't fire on top of it.
  let terminal = false;
  let cancelled = false;
  const flags = { readFailed: false };

  _resumeAutoScroll();
  _streamAbortController = new AbortController();
  _admitting = true; // until the stream's `session` frame
  _stopOnceAdmitted = false;

  try {
    const res = await api.streamChat(
      {
        message,
        agent: store.agent,
        session_id: store.sessionId,
        attachments: attachments?.length ? attachments : undefined,
        context: collectContext(),
        busy,
      },
      { signal: _streamAbortController.signal }
    );

    if (!res.ok || !res.body) {
      terminal = true;
      // The server's message (e.g. "too many concurrent runs") beats a bare
      // status line.
      const err = await apiError(res);
      if (queued) {
        outcome.refused = { code: err.code ?? '', message: err.message };
        return outcome;
      }
      _lastErrorCode = err.code ?? '';
      appendErrorNotice(err.message, err.code);
      appendRetry();
      return;
    }

    runStarted();
    // The run is registered — messages that raced the previous run's end can
    // re-enter the queue now (each as its own turn).
    drainPendingResend();

    for await (const ev of readStream(res, flags)) {
      if (store.chatEpoch !== streamEpoch) {
        _streamAbortController?.abort();
        return outcome;
      }
      await handleEvent(ev);
    }
  } catch (err) {
    // A session switch / new chat detaches by bumping the epoch and aborting
    // the fetch — not an error, and the view we left is gone. Leave it alone.
    if (store.chatEpoch !== streamEpoch) return outcome;
    terminal = true;
    // A queued message that broke off before the server named its run: its
    // fate is unknown, which the queue reports.
    if (queued && !outcome.admitted) return outcome;
    if (err.name === 'AbortError') {
      cancelled = true;
      ensureBody();
      if (!store.rawText) store.rawText = t('chat.cancelled');
      flushRender(true);
    } else {
      ensureBody();
      const raw = err.message ?? String(err);
      const hint = humanizeError(raw, err.code);
      store.rawText += `\n\n> ⚠️ **${t('chat.error')}:** ${hint ? `${hint} (${raw})` : raw}`;
      flushRender(true);
      appendRetry();
    }
  } finally {
    cancelRender();
    _onAdmit = null;
    // If a switch superseded this stream, its DOM/UI now belong to another view;
    // do only the connection-local cleanup above and skip the rest.
    if (store.chatEpoch === streamEpoch) {
      _admitting = false; // e.g. refused: no session frame came
      _stopOnceAdmitted = false;
      if (store.body && store.rawText) {
        store.body.dataset.raw = store.rawText; // store raw markdown for copy
        flushRender(true);
      }
      // Stream ended right after an `error` event → the failure was terminal
      // (a recovered tool error is followed by more events). Offer a Retry.
      if (_lastEventWasError && store.bubble) appendRetry();
      // Clean EOF with no `done` and no surfaced failure: the connection
      // dropped mid-run and the reply on screen may be missing its tail.
      const dropped = !terminal && !_lastEventWasError && !_sawDone;
      if (dropped && !store.sessionId && store.bubble) {
        // Nothing persisted to reconnect to — surface it as a failure.
        appendErrorNotice(t('chat.connectionLost'));
        appendRetry();
      }
      finalizeCurrentAssistantTurn();
      // Un-mute any queued bubbles the server dropped (an errored/cancelled run
      // doesn't auto-chain) so they read as sent — the user can resend.
      flushQueuedTurns();
      exitStreamingUI(runStatus({ cancelled, readFailed: flags.readFailed, terminal }));
      _streamAbortController = null;
      // A new chat's title is generated server-side just after the first turn —
      // poll for it (bounded). Other turns only need one refresh so the session
      // jumps to the top of the list.
      if (store.titlePending) {
        pollForTitle(store.sessionId);
      } else {
        loadSessions();
      }
      if (dropped && store.sessionId) {
        recoverDroppedStream(store.sessionId);
      } else if (!resumePendingResend() && _sawDone && !_lastEventWasError) {
        // Only after a run that actually answered: a cancel or a terminal
        // failure has nothing to follow up on.
        announce(t('a11y.replyDone'));
        fetchFollowups(store.sessionId, _lastReply);
      }
    }
  }
  return outcome;
}

/**
 * Re-attach to an in-progress run for `sessionId` and stream its remaining
 * output into a fresh assistant turn.
 * @param {string} sessionId
 * @returns {Promise<'attached' | 'norun' | 'error'>} 'attached' once the stream
 *   was consumed (however it ended), 'norun' when there was nothing to attach
 *   to, 'error' when the attach request itself failed.
 */
export async function runReconnect(sessionId) {
  enterStreamingUI();
  startAssistantTurn();
  // The run began before this page did: its start is on the live-run row.
  _runStartedAt = store.runsBySession?.get(sessionId)?.started_at ?? 0;
  const streamEpoch = store.chatEpoch;
  _lastEventWasError = false;
  _sawDone = false;
  _lastEvent = '';
  _lastReply = '';
  let terminal = false; // see runStream — gates the dropped-connection check
  let cancelled = false;
  const flags = { readFailed: false };
  let outcome = /** @type {'attached' | 'norun' | 'error'} */ ('attached');

  _resumeAutoScroll();
  _streamAbortController = new AbortController();

  try {
    const res = await api.reconnect(sessionId, {
      signal: _streamAbortController.signal,
    });
    if (store.chatEpoch !== streamEpoch) return 'attached'; // switched away mid-request

    if (!res.ok || !res.body) {
      terminal = true;
      // 404 = nothing to reconnect, 409 = already running or agent gone.
      // Either way: silently remove the empty placeholder and let the user
      // see the already-rendered history without an error message.
      store.turnNode?.remove();
      store.turnNode = null;
      return 'norun';
    }

    runStarted();
    drainPendingResend(); // same re-queue point as runStream

    for await (const ev of readStream(res, flags)) {
      if (store.chatEpoch !== streamEpoch) {
        _streamAbortController?.abort();
        return 'attached';
      }
      await handleEvent(ev);
    }
  } catch (err) {
    if (store.chatEpoch !== streamEpoch) return 'attached'; // detached by a switch — not an error
    terminal = true;
    outcome = 'error';
    cancelled = err.name === 'AbortError';
    if (err.name !== 'AbortError') {
      ensureBody();
      const raw = err.message ?? String(err);
      const hint = humanizeError(raw, err.code);
      store.rawText += `\n\n> ⚠️ **${t('chat.error')}:** ${hint ? `${hint} (${raw})` : raw}`;
      flushRender(true);
    }
  } finally {
    cancelRender();
    // A switch superseded this reconnect: its DOM/UI belong to another view now.
    if (store.chatEpoch === streamEpoch) {
      if (store.body && store.rawText) {
        store.body.dataset.raw = store.rawText;
        flushRender(true);
      }
      // Retry re-sends store.lastMessage — only offer it when there is one
      // (a reconnect after a page refresh has nothing to re-send).
      if (_lastEventWasError && store.bubble && store.lastMessage) appendRetry();
      const dropped = !terminal && !_lastEventWasError && !_sawDone;
      finalizeCurrentAssistantTurn();
      flushQueuedTurns();
      exitStreamingUI(runStatus({ cancelled, readFailed: flags.readFailed, terminal }));
      _streamAbortController = null;
      loadSessions();
      if (dropped) {
        recoverDroppedStream(sessionId);
      } else if (!resumePendingResend() && _sawDone && !_lastEventWasError) {
        announce(t('a11y.replyDone'));
        fetchFollowups(sessionId, _lastReply);
      }
    }
  }
  return outcome;
}

/** Reset the chat view to the empty new-session state; detaches from any live run (which keeps streaming server-side). */
export function resetChatForNewSession() {
  detachStream(); // keep any live run going server-side; just disconnect from it
  resetChatView();
  renderEmptyState();
  hideContextMeter();
  _resumeAutoScroll(); // after the swap, so the reset's scroll event is a no-op
}

/**
 * Detach the client from the in-flight run WITHOUT cancelling it server-side.
 * The supervised run keeps streaming and stays reachable (its sidebar dot
 * persists), so clicking back into the session reconnects to it. Bumps the
 * epoch so the live runStream/runReconnect loop bails and its catch/finally
 * no-op — the view we're moving to now owns the DOM — aborts the SSE fetch (the
 * server treats the dropped connection as a detach), and returns the composer
 * to its idle state. Contrast cancelStream(), which tells the server to stop.
 */
export function detachStream() {
  store.chatEpoch += 1;
  if (_streamAbortController) {
    _streamAbortController.abort();
    _streamAbortController = null;
  }
  cancelRender();
  // The superseded run's finally is now epoch-guarded off, so any *global* state
  // it would have reset has to be reset here or it leaks into the next view:
  //  - queued/pending buffers, else a later run's finally flushes stale bubbles
  //    or resends a raced message into the wrong chat (accepted injects still
  //    replay from the server mailbox on reconnect; an un-accepted one is dropped);
  //  - the todo panel (only renderHistory rebuilds it — a failed switch won't);
  //  - any in-flight follow-up request, whose chips would land in the new view;
  //  - a stop waiting for the run's admission: leaving isn't stopping.
  _queuedTurns = [];
  _pendingResend = [];
  _admitting = false;
  _stopOnceAdmitted = false;
  clearTodoPanel();
  clearFollowups();
  if (store.streaming) exitStreamingUI();
}

/**
 * Cancel the active run server-side (contrast detachStream, which only
 * disconnects) and settle the streaming UI.
 * @returns {Promise<void>}
 */
export async function cancelStream() {
  if (_admitting) {
    // The server is still admitting the run (its run_deps hook may take a
    // moment): there's nothing to stop yet. Stop it once the stream names its
    // session — aborting now would only detach from the run about to start.
    _stopOnceAdmitted = true;
    return;
  }
  // Whatever its record says once the stop lands (#322): messages queued
  // behind it wait for the user, not for a `completed`.
  if (_shownRunId) sendQueue.markStopped(_shownRunId);
  if (_streamAbortController) _streamAbortController.abort();
  if (store.sessionId) {
    try {
      await api.cancel(store.sessionId);
    } catch { /* ignore */ }
  }
}

// ---- Composer ----------------------------------------------------------
const sendBtn = /** @type {HTMLButtonElement | null} */ (document.getElementById('send'));
const autoresize = () => {
  if (!promptEl) return;
  promptEl.style.height = 'auto';
  promptEl.style.height = Math.min(promptEl.scrollHeight, window.innerHeight * 0.3) + 'px';
};

/**
 * Put `text` in the composer for the user to edit and send — what a starter
 * prompt does, and window.lovia.setDraft.
 * @param {string} text
 */
export function setDraft(text) {
  if (!promptEl) return;
  promptEl.value = String(text ?? '');
  autoresize();
  updateSendEnabled();
  saveDraftSoon();
  promptEl.focus();
}

// ---- Drafts --------------------------------------------------------------
// The composer's unsent text survives reloads and follows its chat: one
// localStorage slot per session (plus one for the new-chat state), written on
// a short debounce and cleared the moment the text is actually sent/queued.
const DRAFT_PREFIX = 'lovia-draft:';
const DRAFT_TTL_MS = 14 * 24 * 3600 * 1000; // drafts this stale get pruned at boot
const DRAFT_DEBOUNCE_MS = 250;

const draftKey = () => DRAFT_PREFIX + (store.sessionId || 'new');
let _draftTimer = null;
let _draftPendingKey = null; // captured at typing time — a switch must not retarget it

function saveDraftSoon() {
  _draftPendingKey = draftKey();
  clearTimeout(_draftTimer);
  _draftTimer = setTimeout(flushDraft, DRAFT_DEBOUNCE_MS);
}

function flushDraft() {
  clearTimeout(_draftTimer);
  _draftTimer = null;
  const key = _draftPendingKey;
  _draftPendingKey = null;
  if (!key) return;
  const text = promptEl?.value ?? '';
  try {
    if (text.trim()) {
      localStorage.setItem(key, JSON.stringify({ text, ts: Date.now() }));
    } else {
      localStorage.removeItem(key);
    }
  } catch { /* storage unavailable — drafts degrade off */ }
}

// After a send: the pending timer must not resurrect the just-sent text.
function clearDraft() {
  clearTimeout(_draftTimer);
  _draftTimer = null;
  _draftPendingKey = null;
  try {
    localStorage.removeItem(draftKey());
  } catch { /* ignore */ }
}

function restoreDraft() {
  if (!promptEl) return;
  let text = '';
  try {
    const raw = localStorage.getItem(draftKey());
    if (raw) text = JSON.parse(raw).text || '';
  } catch { /* corrupt or unavailable — start blank */ }
  promptEl.value = text;
  autoresize();
  updateSendEnabled();
}

function pruneDrafts() {
  try {
    const cutoff = Date.now() - DRAFT_TTL_MS;
    const stale = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key?.startsWith(DRAFT_PREFIX)) continue;
      try {
        const { ts } = JSON.parse(localStorage.getItem(key) || '{}');
        if (!ts || ts < cutoff) stale.push(key);
      } catch {
        stale.push(key);
      }
    }
    stale.forEach((k) => localStorage.removeItem(k));
  } catch { /* storage unavailable */ }
}

// Save under the chat being left (the pending key points there), then load
// the one being entered. Registered here, next to the state it manages.
store.on('session-switched', () => {
  flushDraft();
  restoreDraft();
});
store.on('reset-chat-view', () => {
  flushDraft();
  restoreDraft();
});
store.on('session-deleted', (id) => {
  try {
    localStorage.removeItem(DRAFT_PREFIX + id);
  } catch { /* ignore */ }
  dropFollowups(id);
});

// ---- Attachments -------------------------------------------------------
// The "+" button, paste, and drag-drop upload images/files to the agent's
// workspace (POST /api/workspace/upload); they ride the next send as
// ChatRequest.attachments. Gated on the agent having a workspace — the same
// switch as the Files panel. Mid-run they ride a queued message (a run of its
// own); a steer is text-only (/api/chat/inject takes no attachments).
let _pendingAttachments = []; // uploaded { path, name, mime, kind, size } (+ uploading?)

function attachEnabled() {
  return !!store.agents.find((a) => a.name === store.agent)?.workspace;
}

// Past this size a paste stops being a message and starts being a document.
const PASTE_ATTACH_CHARS = 4000;
const PASTE_ATTACH_LINES = 60;

function pastedTextFile(text) {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const stamp =
    `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}` +
    `-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  return new File([text], `pasted-${stamp}.txt`, { type: 'text/plain' });
}

function pendingReady() {
  return _pendingAttachments.filter((a) => !a.uploading && a.path);
}

/** What ChatRequest.attachments carries for the tray's chips. */
function attachmentPayload(/** @type {any[]} */ attachments) {
  return attachments.map((a) => ({ path: a.path, mime: a.mime, kind: a.kind, name: a.name }));
}

// An upload still in flight holds Send: sending without it would drop it.
function uploading() {
  return _pendingAttachments.some((a) => a.uploading);
}

// Nothing typed and nothing attached: the composer can take an item back.
function composerEmpty() {
  return !promptEl?.value.trim() && !_pendingAttachments.length;
}

function updateSendEnabled() {
  if (!sendBtn) return;
  const hasText = !!promptEl?.value.trim();
  const hasContent = hasText || pendingReady().length > 0;
  const waiting = uploading();
  if (store.streaming) {
    // Stop owns the slot; Send reappears only once there's something to
    // queue — a greyed Send next to Stop reads as two competing controls.
    // What Send does now, said on the button too: the placeholder that says
    // it is gone once there is text. Queuing waits for the run to be named.
    sendBtn.hidden = !(hasContent || waiting);
    sendBtn.disabled = !hasContent || waiting || !_shownRunId;
    sendBtn.title = t(waiting ? 'composer.waitUploads' : 'composer.queueTip');
    sendBtn.setAttribute('aria-label', t('composer.queueSend'));
  } else {
    sendBtn.hidden = false;
    sendBtn.disabled = !hasContent || waiting;
    sendBtn.title = t(waiting ? 'composer.waitUploads' : 'composer.sendTip');
    sendBtn.setAttribute('aria-label', t('composer.send'));
  }
  syncQueueEdit();
}

function syncAttachButton() {
  const btn = /** @type {HTMLButtonElement | null} */ (document.getElementById('attach'));
  if (!btn) return;
  const ok = attachEnabled();
  btn.hidden = !ok;
  btn.disabled = !ok;
  if (!ok && _pendingAttachments.length) {
    _pendingAttachments = [];
    renderAttachTray();
    updateSendEnabled();
  }
}

function renderAttachTray() {
  const tray = document.getElementById('attach-tray');
  if (!tray) return;
  tray.replaceChildren();
  if (!_pendingAttachments.length) {
    tray.hidden = true;
    return;
  }
  tray.hidden = false;
  _pendingAttachments.forEach((att, i) => {
    tray.appendChild(
      makeAttachChip(att, () => {
        _pendingAttachments.splice(i, 1);
        renderAttachTray();
        updateSendEnabled();
      }),
    );
  });
}

function makeAttachChip(att, onRemove) {
  const chip = document.createElement('div');
  chip.className = 'attach-chip' + (att.uploading ? ' uploading' : '');
  if (att.kind === 'image' && att.path) {
    const img = document.createElement('img');
    img.className = 'attach-thumb';
    img.src = api.workspaceRawUrl({ agent: store.agent, path: att.path });
    img.alt = att.name || '';
    chip.appendChild(img);
  } else {
    const ic = document.createElement('span');
    ic.className = 'attach-ic';
    ic.innerHTML = icon(att.kind === 'image' ? 'image' : 'file-text', { size: 16 });
    chip.appendChild(ic);
  }
  const name = document.createElement('span');
  name.className = 'attach-name';
  name.textContent = att.name || att.path || '';
  name.title = name.textContent;
  chip.appendChild(name);
  const rm = document.createElement('button');
  rm.type = 'button';
  rm.className = 'attach-remove';
  rm.setAttribute('aria-label', t('composer.removeAttachment'));
  rm.innerHTML = icon('x', { size: 13 });
  rm.addEventListener('click', onRemove);
  chip.appendChild(rm);
  return chip;
}

async function uploadFiles(files) {
  if (!attachEnabled()) {
    toast(t('composer.attachNoWorkspace'), { type: 'error' });
    return;
  }
  for (const file of files) {
    const chip = {
      uploading: true,
      name: file.name,
      kind: file.type?.startsWith('image/') ? 'image' : 'file',
    };
    _pendingAttachments.push(chip);
    renderAttachTray();
    updateSendEnabled(); // Send waits for it
    try {
      Object.assign(chip, await api.uploadFile(file, { agent: store.agent }), {
        uploading: false,
      });
    } catch {
      const i = _pendingAttachments.indexOf(chip);
      if (i >= 0) _pendingAttachments.splice(i, 1);
      toast(t('composer.uploadFailed', { name: file.name }), { type: 'error' });
    }
    renderAttachTray();
  }
  updateSendEnabled();
}

// Attachment views inside a user bubble (live send + history replay).
function makeAttachmentsBlock(attachments) {
  const wrap = document.createElement('div');
  wrap.className = 'msg-attachments';
  for (const att of attachments) wrap.appendChild(makeAttachmentView(att));
  return wrap;
}

function makeAttachmentView(att) {
  const url = att.path
    ? api.workspaceRawUrl({ agent: store.agent, path: att.path })
    : att.src || null;
  if (att.kind === 'image' && url) {
    // Click opens the image in an in-app lightbox rather than a new tab. The
    // href stays set so cmd/ctrl-click can still open the raw image, and so it
    // degrades to a plain link without JS.
    const a = document.createElement('a');
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.className = 'msg-attach-image';
    a.addEventListener('click', (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      const downloadHref = att.path
        ? api.workspaceRawUrl({ agent: store.agent, path: att.path, download: true })
        : url;
      openImageLightbox(url, { alt: att.name || '', downloadHref });
    });
    const img = document.createElement('img');
    img.src = url;
    img.alt = att.name || '';
    img.loading = 'lazy';
    a.appendChild(img);
    return a;
  }
  // A workspace file opens in the Files panel (rendered in-app: text, md, csv,
  // code, or a download for binaries) instead of forcing a browser download.
  const a = document.createElement('a');
  a.className = 'msg-attach-file';
  if (att.path) {
    a.href = api.workspaceRawUrl({ agent: store.agent, path: att.path, download: true });
    a.addEventListener('click', (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      store.emit('open-workspace-file', { path: att.path });
    });
  }
  a.innerHTML = icon('file-text', { size: 15 });
  const name = document.createElement('span');
  name.textContent = att.name || att.path || 'file';
  a.appendChild(name);
  return a;
}

// A turn's attachments survive in stored history only as the note the server
// appends to the user text (mirrors `_attachment_note` in
// lovia/web/attachments.py — keep in sync). Split that text back into the
// visible message and renderable attachment views (image vs file inferred from
// the extension, like the Files panel), so a reloaded turn shows previews
// instead of the raw "[Attached in the workspace: …]" line. Works whether the
// image round-tripped inline (vision) or the turn flattened to text.
const ATTACHMENT_NOTE_RE =
  /\n*\[Attached in the workspace: (.+?)\. Read with your file tools when relevant\.\]\s*$/;

function splitAttachmentNote(text) {
  if (typeof text !== 'string') return { text: '', attachments: [] };
  const m = text.match(ATTACHMENT_NOTE_RE);
  if (!m) return { text, attachments: [] };
  // ", " is an unambiguous separator here: composer uploads are sanitized
  // server-side (_safe_upload_name replaces commas with "_"), so a stored name
  // can hold a space but never a comma.
  const attachments = m[1]
    .split(', ')
    .map((p) => p.trim())
    .filter(Boolean)
    .map((path) => ({
      path,
      kind: isImagePath(path) ? 'image' : 'file',
      name: path.split('/').pop() || path,
    }));
  return { text: text.slice(0, m.index).trimEnd(), attachments };
}

/** Wire up the composer: submit, Enter-to-send behavior, attachments, and drag-and-drop. */
/**
 * Send a text-only user message as the composer would — queued into a live
 * run, or as a fresh turn — without touching the draft or pending
 * attachments. The composer and UI components (`ctx.send`) share it.
 * @param {string} message Trimmed, non-empty.
 */
/**
 * Send `message` as the user — or, mid-run, queue it for the run's next turn.
 * Shared by UI components' ctx.send and window.lovia.send.
 * @param {string} message
 */
export async function sendText(message) {
  _resumeAutoScroll();
  if (!store.streaming) {
    document.getElementById('empty-state')?.remove();
    appendUserTurn(message);
    await runStream(message);
    return;
  }
  const sid = store.sessionId;
  const after = _shownRunId;
  const { node, outcome } = await steer(message);
  // Raced the run's end: keep the bubble muted and deliver the message
  // as its own turn once the stream settles — the next run re-injects it
  // (drainPendingResend) or it seeds a fresh run (resumePendingResend).
  if (outcome === 'refused') _pendingResend.push({ message, node });
  else if (outcome === 'unknown' && sid) parkUnsure(sid, node, message, after);
}

/**
 * A steer that got no answer may sit in the run already — its drain then
 * draws it from the server — so it is never resent on our own: it waits at
 * the head of the queue strip, paused as unknown, for the user to check the
 * chat and send again or withdraw (#320).
 * @param {string} sid
 * @param {HTMLElement | null} node Its muted bubble.
 * @param {string} message
 * @param {string | null} after
 */
function parkUnsure(sid, node, message, after) {
  node?.remove();
  sendQueue.putBack(sid, message, after, true);
}

/**
 * Steer `message` into the live run: the server drains it at the next turn
 * start, or seeds the next run if this one ends first. A muted bubble (with a
 * cancel affordance) shows it until the run confirms it or the user withdraws
 * it. Unless `accepted`, the caller owns the bubble: `refused` — no run took
 * it; `unknown` — no answer (network, or a 5xx a proxy may have sent after
 * the server took it), so the run may hold it.
 * @param {string} message
 * @param {HTMLElement | null} [node] Its muted bubble, if already drawn.
 * @returns {Promise<{ node: HTMLElement | null, outcome: 'accepted' | 'refused' | 'unknown' }>}
 */
async function steer(message, node = appendUserTurn(message, { queued: true })) {
  if (node) _queuedTurns.push(node); // before the await: its drain may beat the answer
  /** @type {'accepted' | 'refused' | 'unknown'} */
  let outcome = 'refused';
  try {
    const res = await api.inject({ session_id: store.sessionId, message });
    if (res?.accepted) {
      if (node) addWithdrawButton(node, res.id);
      return { node, outcome: 'accepted' };
    }
  } catch (err) {
    const status = /** @type {{ status?: number }} */ (err).status;
    if (!(status && status < 500)) outcome = 'unknown';
  }
  // The run drew it before the answer came back: that is the answer.
  if (node && _drained.has(node)) return { node, outcome: 'accepted' };
  const i = _queuedTurns.indexOf(node);
  if (i >= 0) _queuedTurns.splice(i, 1);
  return { node, outcome };
}

// ---- Queued messages: sent once the run on screen completes (#308) -------
// While a run streams, the composer queues a message to start a run of its
// own after this one completes (send-queue.js), in the strip above the input.
// Steering — the message joins the run's next turn (above) — is decided per
// message: ⌥/Alt+Enter as it is sent, or the strip's Steer once queued.
const queueEl = document.getElementById('send-queue');
let _steerOnce = false;
// The open chat is drawn, not mid-switch: a queued message may join it.
let _viewReady = true;

/**
 * Steer a queued item into the live run. Out of the queue first, so the queue
 * can't also send it; back to its head if no run took it. With no answer the
 * run may hold it — its drain then draws it from the server — so it goes back
 * paused as unknown: resending on our own could run it twice.
 * @param {string} sid
 * @param {number} id
 */
async function steerQueued(sid, id) {
  const taken = sendQueue.withdraw(sid, id);
  if (!taken) return;
  _resumeAutoScroll();
  const { node, outcome } = await steer(taken.item.text);
  if (outcome === 'accepted') return;
  if (outcome === 'unknown') {
    parkUnsure(sid, node, taken.item.text, taken.after);
    return;
  }
  node?.remove();
  sendQueue.putBack(sid, taken.item.text, taken.after);
}

/** Back into the empty composer — text and attachments — to edit and resend. */
function editQueued(/** @type {string} */ sid, /** @type {number} */ id) {
  if (!composerEmpty()) return; // never over a draft
  const taken = sendQueue.withdraw(sid, id);
  if (!taken) return;
  _pendingAttachments = [...taken.item.attachments];
  renderAttachTray();
  setDraft(taken.item.text);
}

/**
 * Send a queued message as its own run (busy="reject"): a muted turn until
 * the server admits it, gone if it is refused.
 * @param {string} sid
 * @param {import('./send-queue.js').QueuedItem} item
 * @returns {Promise<import('./send-queue.js').SendResult>}
 */
function sendQueued(sid, { text, attachments }) {
  return new Promise((resolve) => {
    if (store.sessionId !== sid) {
      resolve('busy'); // left between the check and the send: ask again later
      return;
    }
    document.getElementById('empty-state')?.remove();
    _resumeAutoScroll();
    const node = appendUserTurn(text, { queued: true, attachments });
    void runStream(text, attachmentPayload(attachments), {
      busy: 'reject',
      onAdmit: (runId) => {
        confirmQueuedTurn(node);
        resolve({ runId });
      },
    }).then((outcome) => {
      if (outcome.admitted) return;
      node?.remove();
      if (outcome.refused?.code === 'run_active') {
        resolve('busy');
      } else if (outcome.refused) {
        toast(outcome.refused.message, { type: 'error' });
        resolve({ refused: outcome.refused.message });
      } else {
        resolve('unknown');
      }
    });
  });
}

const sendQueue = createSendQueue({
  liveRuns: () => api.listRuns(),
  runRecord: async (runId) => (await api.runHistory({ run_id: runId, limit: 1 }))[0] ?? null,
  ready: (sid) => _viewReady && store.sessionId === sid && !store.streaming,
  send: sendQueued,
  changed: (sid) => {
    if (sid === store.sessionId) renderQueue();
  },
});

/** The open chat's queue, above the composer. */
function renderQueue() {
  if (!queueEl) return;
  const sid = store.sessionId;
  const { items, paused, sending } = sid
    ? sendQueue.view(sid)
    : { items: [], paused: null, sending: null };
  queueEl.hidden = !items.length;
  queueEl.replaceChildren();
  if (!sid || !items.length) return;
  if (paused) {
    const note = document.createElement('div');
    note.className = 'send-queue-note';
    const text = document.createElement('span');
    text.textContent = t(paused === 'unknown' ? 'queue.unknown' : 'queue.paused');
    const go = document.createElement('button');
    go.type = 'button';
    go.className = 'btn btn-ghost btn-sm';
    go.textContent = t(paused === 'unknown' ? 'queue.resend' : 'queue.sendNow');
    go.addEventListener('click', () => sendQueue.sendNow(sid));
    note.append(text, go);
    queueEl.append(note);
  }
  const button = (/** @type {string} */ name, /** @type {string} */ key, /** @type {() => void} */ act) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'send-queue-act';
    btn.innerHTML = icon(name, { size: 13 });
    btn.title = t(key);
    btn.setAttribute('aria-label', t(key));
    btn.addEventListener('click', act);
    return btn;
  };
  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'send-queue-item';
    row.innerHTML = icon('clock', { size: 13 });
    const names = item.attachments.map((a) => a.name || a.path).join(' · ');
    const text = document.createElement('span');
    text.className = 'send-queue-text';
    // Attachments only: their names say what the message is.
    text.classList.toggle('muted', !item.text);
    text.textContent = item.text || names;
    text.title = text.textContent;
    row.append(text);
    if (item.attachments.length) row.append(queuedAttachments(item.attachments, names));
    const onItsWay = sending === item.id;
    row.classList.toggle('sending', onItsWay);
    // Steering needs a run live in this chat to take it, and is text-only.
    if (store.streaming && !onItsWay && !item.attachments.length) {
      const go = button('zap', 'queue.steerTip', () => void steerQueued(sid, item.id));
      go.classList.add('send-queue-steer');
      go.insertAdjacentText('beforeend', t('queue.steer'));
      row.append(go);
    }
    const edit = button('pencil', 'queue.edit', () => editQueued(sid, item.id));
    edit.classList.add('send-queue-edit');
    edit.disabled = onItsWay || !composerEmpty();
    const drop = button('x', 'queue.withdraw', () => sendQueue.withdraw(sid, item.id));
    drop.disabled = onItsWay;
    row.append(edit, drop);
    queueEl.append(row);
  }
}

/**
 * A queued item's attachments, one line high: up to three image thumbnails,
 * then the rest as one count — so a row never grows past its text.
 * @param {any[]} atts
 * @param {string} names
 */
function queuedAttachments(atts, names) {
  const wrap = document.createElement('span');
  wrap.className = 'send-queue-atts';
  wrap.title = names;
  wrap.setAttribute('role', 'img');
  wrap.setAttribute('aria-label', t('queue.attachments', { n: atts.length, names }));
  const thumbs = atts.filter((a) => a.kind === 'image' && a.path).slice(0, 3);
  for (const att of thumbs) {
    const img = document.createElement('img');
    img.className = 'send-queue-thumb';
    img.src = api.workspaceRawUrl({ agent: store.agent, path: att.path });
    img.alt = '';
    wrap.append(img);
  }
  const rest = atts.length - thumbs.length;
  if (rest) {
    const more = document.createElement('span');
    more.className = 'send-queue-more';
    // After thumbnails "+N"; files alone, a file icon (and their count).
    if (thumbs.length) more.textContent = `+${rest}`;
    else more.innerHTML = icon('file-text', { size: 13 }) + (rest > 1 ? String(rest) : '');
    wrap.append(more);
  }
  return wrap;
}

/** Edit only into an empty composer: re-checked as the draft changes. */
function syncQueueEdit() {
  const busy = !composerEmpty();
  queueEl?.querySelectorAll('.send-queue-edit').forEach((btn) => {
    if (btn instanceof HTMLButtonElement) {
      btn.disabled = busy || !!btn.closest('.send-queue-item.sending');
    }
  });
}

// Every wake-up goes through the queue's one pump, which asks the server
// afresh: the view's own stream ending (after its cleanup), any run starting
// or ending (/api/events, and its reconnects), the chat drawn after a switch.
store.on('run-end', ({ sessionId }) => {
  if (sessionId) void sendQueue.pump(sessionId);
});
store.on('runs-changed', () => {
  if (store.sessionId) void sendQueue.pump(store.sessionId);
});
store.on('session-switched', () => {
  _viewReady = false;
  renderQueue();
});
store.on('session-loaded', (id) => {
  _viewReady = true;
  renderQueue();
  void sendQueue.pump(id);
});
store.on('reset-chat-view', () => {
  _viewReady = true;
  renderQueue();
});

// UI components (components.js) see the page through this context, and may
// post a message for the user — only from a real click/keypress, so a
// component can never send on its own (e.g. while rendering).
configureComponents({
  context: () => ({
    theme: store.theme === 'dark' ? 'dark' : 'light',
    lang: currentLang(),
    basePath: basePath(),
    sessionId: store.sessionId ?? null,
  }),
  send: (text) => {
    const message = typeof text === 'string' ? text.trim() : '';
    // Fail closed: a browser without UserActivation can't vouch for a gesture.
    if (!message || navigator.userActivation?.isActive !== true) return false;
    void sendText(message);
    return true;
  },
  label: (key, kind) => t(`ui.${key}`, { kind }),
});
watchComponents(document.getElementById('transcript'));
store.on('theme-changed', rerenderComponents);

export function initComposer() {
  initContextRing();

  // Attachments: "+" opens a hidden picker; paste and drag-drop also upload.
  const attachInput = document.createElement('input');
  attachInput.type = 'file';
  attachInput.multiple = true;
  attachInput.style.display = 'none';
  document.body.appendChild(attachInput);
  attachInput.addEventListener('change', () => {
    if (attachInput.files?.length) uploadFiles([...attachInput.files]);
    attachInput.value = ''; // let the same file be picked again
  });
  document.getElementById('attach')?.addEventListener('click', () => {
    if (attachEnabled()) attachInput.click();
  });
  promptEl?.addEventListener('paste', (e) => {
    if (!attachEnabled()) return;
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) {
      e.preventDefault();
      uploadFiles(files);
      return;
    }
    // A paste the size of a document would drown the composer — hand it to
    // the attachment pipeline as a text file instead (the agent reads it from
    // the workspace), leaving whatever was already typed intact.
    const text = e.clipboardData?.getData('text/plain') ?? '';
    if (
      text.length > PASTE_ATTACH_CHARS ||
      text.split('\n').length > PASTE_ATTACH_LINES
    ) {
      e.preventDefault();
      uploadFiles([pastedTextFile(text)]);
    }
  });
  if (composer) {
    composer.addEventListener('dragover', (e) => {
      if (!attachEnabled()) return;
      if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
      e.preventDefault();
      composer.classList.add('drag-over');
    });
    composer.addEventListener('dragleave', (e) => {
      if (!composer.contains(/** @type {Node} */ (e.relatedTarget))) composer.classList.remove('drag-over');
    });
    composer.addEventListener('drop', (e) => {
      composer.classList.remove('drag-over');
      if (!attachEnabled()) return;
      const files = [...(e.dataTransfer?.files || [])];
      if (files.length) {
        e.preventDefault();
        uploadFiles(files);
      }
    });
  }
  store.on('agents-loaded', syncAttachButton);
  store.on('agent-changed', syncAttachButton);
  syncAttachButton();

  // Files panel → composer: reference an existing workspace file on the next
  // message. No upload — the file is already under the workspace root; the
  // server re-derives kind/mime from the file on disk anyway.
  store.on('attach-workspace-file', ({ path, name, kind }) => {
    if (!attachEnabled()) return;
    if (!_pendingAttachments.some((a) => a.path === path)) {
      _pendingAttachments.push({ path, name, kind, mime: 'application/octet-stream' });
      renderAttachTray();
      updateSendEnabled();
    }
    promptEl?.focus();
  });

  // On touch devices Enter always inserts a newline (there's no Shift key to
  // combine with) and the send button does the sending. On desktop the "Enter
  // key" preference decides: send-on-Enter (Shift+Enter = newline), or
  // newline-on-Enter (⌘/Ctrl+Enter sends). `isComposing` guards an IME session
  // so confirming CJK candidates with Enter never fires a send.
  const coarsePointer = window.matchMedia('(pointer: coarse)').matches;
  promptEl?.addEventListener('input', () => {
    autoresize();
    updateSendEnabled();
    saveDraftSoon();
  });
  pruneDrafts();
  restoreDraft(); // the new-chat draft; a URL-restored session re-restores on switch
  promptEl?.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.isComposing || coarsePointer) return;
    // ⌥/Alt+Enter mid-run: steer this message instead of queuing it,
    // whichever Enter preference is set.
    if (e.altKey && store.streaming) {
      e.preventDefault();
      _steerOnce = true;
      composer?.requestSubmit();
      return;
    }
    const withMod = e.metaKey || e.ctrlKey;
    const send = enterToSend() ? !e.shiftKey && !withMod : withMod;
    if (send) {
      e.preventDefault();
      composer?.requestSubmit();
    }
  });

  // Example prompts (server-rendered or renderEmptyState's) fill the
  // composer for editing — clicking must not fire a send behind your back.
  document.getElementById('transcript')?.addEventListener('click', (e) => {
    const btn = e.target instanceof Element ? e.target.closest('.empty-example') : null;
    if (btn) setDraft(btn.textContent);
  });

  composer?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const message = promptEl.value.trim();

    // An upload in flight holds the send (Send is disabled), whichever key.
    if (uploading()) {
      _steerOnce = false;
      return;
    }
    const attachments = pendingReady();

    if (store.streaming) {
      const queue = !_steerOnce;
      _steerOnce = false;
      if (!message && !attachments.length) return;
      // A steer is text-only: never split a message into a steered text and
      // files left behind — keep it all, and say how it can go.
      if (!queue && attachments.length) {
        toast(t('composer.steerNoAttach'), { key: 'steer-no-attach', timeout: 3000 });
        return;
      }
      // A message queues behind the run on screen, which its stream names
      // as it is admitted: until then it stays put (Send is disabled).
      if (queue && !(store.sessionId && _shownRunId)) return;
      promptEl.value = '';
      autoresize();
      _pendingAttachments = [];
      renderAttachTray();
      updateSendEnabled();
      clearDraft();
      if (queue) {
        sendQueue.enqueue(store.sessionId, message, _shownRunId, attachments);
        return;
      }
      await sendText(message);
      return;
    }
    _steerOnce = false;

    if (!message && !attachments.length) return;
    const payload = attachmentPayload(attachments);
    promptEl.value = '';
    autoresize();
    _pendingAttachments = [];
    renderAttachTray();
    updateSendEnabled();
    clearDraft();
    _resumeAutoScroll();

    document.getElementById('empty-state')?.remove();
    appendUserTurn(message, { attachments });
    await runStream(message, payload);
  });

  stopBtn?.addEventListener('click', () => cancelStream());
  // Computed on hover/focus: no ticking timer for a figure few look at.
  const refreshStopTip = () => {
    if (!stopBtn) return;
    const tip = t('composer.stopTip');
    stopBtn.title = _runStartedAt
      ? `${t('chat.runningFor', { d: formatDuration(Date.now() / 1000 - _runStartedAt) })} · ${tip}`
      : tip;
  };
  stopBtn?.addEventListener('mouseenter', refreshStopTip);
  stopBtn?.addEventListener('focus', refreshStopTip);
}
