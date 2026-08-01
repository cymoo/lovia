// Chat streaming, SSE handling, message rendering.
import { t } from './i18n.js';
import { store } from './store.js';
import { toast } from './toast.js';
import { api, readSSE } from './api.js';
import { copyToClipboard, openImageLightbox } from './ui.js';
import { loadSessions } from './sessions.js';
import { renderMermaid } from './diagrams.js';
import { icon } from './icons.js';
import { enterToSend, followupsEnabled } from './settings.js';
import { readFollowups, writeFollowups, dropFollowups } from './followup-cache.js';
import {
  escapeHtml,
  formatDateTime,
  formatTimeSmart,
  highlightIn,
  isImagePath,
  renderMarkdown,
  toDate,
} from './util.js';

// ---- Markdown & Highlighting -------------------------------------------
// marked / DOMPurify / hljs / mermaid arrive from CDN <script> tags and may
// be absent (offline, blocked CDN, SRI failure). Rendering helpers live in
// util.js (shared with the Files panel) and degrade to escaped text.
if (typeof marked !== 'undefined') marked.setOptions({ gfm: true, breaks: false });

// Escape arbitrary text and turn bare http(s) URLs into clickable links. Used
// for tool-result <pre> blocks so links work without markdown-rendering (which
// would mangle code / shell output); every non-URL character is escaped.
function linkifyText(text) {
  const urlRe = /https?:\/\/[^\s<>"')\]]+/g;
  let out = '';
  let last = 0;
  let m;
  while ((m = urlRe.exec(text)) !== null) {
    out += escapeHtml(text.slice(last, m.index));
    const url = m[0];
    out += `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>`;
    last = m.index + url.length;
  }
  out += escapeHtml(text.slice(last));
  return out;
}

function highlightCode(container) {
  highlightIn(container); // shared cached hljs pass (util.js)
  // Copy buttons + language labels don't need hljs — keep them offline.
  addCodeBlockControls(container);
}

// Tell the Files panel a workspace tool wrote a file (decoupled via store).
function emitWorkspaceTouch(name, args) {
  if (name !== 'write_file' && name !== 'edit_file') return;
  try {
    const path = JSON.parse(args || '{}').path;
    if (path) store.emit('workspace-file-touched', { path });
  } catch { /* malformed args — nothing to signal */ }
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

// Debounced streaming render
let _renderTimer = null;
function scheduleRender() {
  clearTimeout(_renderTimer);
  _renderTimer = setTimeout(flushRender, 60);
}

// True while the user has an active (non-collapsed) selection inside `node`.
function selectionInside(node) {
  const sel = document.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return false;
  return node.contains(sel.getRangeAt(0).commonAncestorContainer);
}

// `force` bypasses the selection guard — end-of-turn flushes must land even
// mid-selection, or the bubble would freeze on stale content.
function flushRender(force = false) {
  if (!store.body || !store.rawText) return;
  // Replacing innerHTML destroys a selection in progress — copying from a
  // streaming reply would be impossible. Skip this flush: the next delta
  // (streaming keeps them coming) or the turn's final, forced flush repaints,
  // so no self-reschedule is needed while the selection is held.
  if (!force && selectionInside(store.body)) return;
  store.body.dataset.raw = store.rawText;
  store.body.innerHTML = renderMarkdown(store.rawText);
  highlightCode(store.body);
  renderMermaid(store.body);
  scrollDown();
}

// ---- Templates ---------------------------------------------------------
/**
 * Deep-clone a `<template>`'s first element child.
 * @param {string} id Template element id.
 * @returns {HTMLElement}
 */
function cloneTemplate(id) {
  const tmpl = /** @type {HTMLTemplateElement} */ (document.getElementById(id));
  return /** @type {HTMLElement} */ (tmpl.content.firstElementChild.cloneNode(true));
}

function makeTurn(role, ts) {
  const node = cloneTemplate('tmpl-turn');
  node.classList.add(role);
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

function argValue(v) {
  if (typeof v === 'string') {
    const oneLine = v.replace(/\s+/g, ' ').trim();
    return oneLine.length > 60 ? `${oneLine.slice(0, 59)}…` : oneLine;
  }
  return JSON.stringify(v);
}

// old/new pairs (edit_file) ARE a diff: the arguments are already the two
// sides of the change, so no diff algorithm is needed to present them as one.
function isDiffArgs(obj) {
  return typeof obj.old === 'string' && typeof obj.new === 'string';
}

// Arguments in render order. `arguments` is the raw JSON the model emitted and
// its key order is not stable — the same call can come back path-first once and
// new-before-old the next time. Two keys are pinned so a card always reads the
// same way: `path` leads (it is the subject of the call, and the first declared
// parameter of every workspace tool that takes one), and a diff pair renders
// old-then-new at whichever side the model mentioned first. Every other key
// keeps the order it came in.
function argEntries(obj) {
  const entries = Object.entries(obj);
  const diff = isDiffArgs(obj);
  const lead = typeof obj.path === 'string';
  if (!diff && !lead) return entries;
  const ordered = [];
  let placed = false;
  for (const [k, v] of entries) {
    if (lead && k === 'path') continue; // hoisted to the front below
    if (diff && (k === 'old' || k === 'new')) {
      if (placed) continue;
      placed = true;
      ordered.push(['old', obj.old], ['new', obj.new]);
      continue;
    }
    ordered.push([k, v]);
  }
  if (lead) ordered.unshift(['path', obj.path]);
  return ordered;
}

// A one-line `(k: v, …)` preview for the tool bubble's summary. The full
// values live in the expanded card's params rows (fillParams).
function formatArgs(args) {
  if (!args) return '()';
  let obj;
  try {
    obj = JSON.parse(args);
  } catch {
    return `(${args})`;
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return `(${args})`;
  const entries = argEntries(obj);
  if (entries.length === 0) return '()';
  return `(${entries.map(([k, v]) => `${k}: ${argValue(v)}`).join(', ')})`;
}

// Full arguments as key/value rows — the one renderer behind both the
// expanded tool card and the approval card. Values stay plain text: args are
// model *inputs*, so no linkification. Short values sit inline next to their
// key; multi-line or long ones become full-width scrollable blocks. Empty
// args append nothing, leaving the container :empty so CSS hides it.
function fillParams(container, args) {
  if (!container || !args) return;
  const addBlock = (text) => {
    const div = document.createElement('div');
    div.className = 'param-val block';
    div.textContent = text;
    container.appendChild(div);
  };
  let obj;
  try {
    obj = JSON.parse(args);
  } catch {
    addBlock(String(args)); // unparsable — show the raw payload
    return;
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    addBlock(String(args));
    return;
  }
  // Color the two sides of a diff so an approval is reviewed as a change,
  // not as two look-alike walls of text.
  const isDiff = isDiffArgs(obj);
  for (const [k, v] of argEntries(obj)) {
    if (isDiff && (k === 'old' || k === 'new')) {
      const old = k === 'old';
      const key = document.createElement('div');
      key.className = 'param-key';
      key.textContent = old ? t('tool.old') : t('tool.new');
      const value = document.createElement('div');
      value.className = `param-val block ${old ? 'diff-old' : 'diff-new'}`;
      value.textContent = v;
      container.append(key, value);
      continue;
    }
    let val = typeof v === 'string' ? v : JSON.stringify(v);
    const block = val.includes('\n') || val.length > 80;
    if (block && typeof v !== 'string') val = JSON.stringify(v, null, 2);
    const key = document.createElement('div');
    key.className = 'param-key';
    key.textContent = k;
    const value = document.createElement('div');
    value.className = block ? 'param-val block' : 'param-val';
    value.textContent = val;
    container.append(key, value);
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
  const footer = bubble.querySelector(':scope > .turn-footer');
  if (footer) {
    bubble.insertBefore(node, footer);
  } else {
    bubble.appendChild(node);
  }
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
// User turns carry their 0-based ordinal (dataset.userTurn) — the currency of
// POST /sessions/{id}/rewind. History renders assign it from the full entry
// list; live sends take the next number chronologically (queued bubbles get
// theirs only once the run confirms them, matching server transcript order).
let _userTurnCount = 0;

async function rewindTo(userTurn, message) {
  try {
    const res = await api.rewindSession(store.sessionId, userTurn);
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
    if (!(await rewindTo(Number(node.dataset.userTurn), text))) {
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
    if (text) rewindTo(Number(lastUser.dataset.userTurn), text);
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
    addCopyButton(bubble); // queued bubbles get their copy button on confirm
    // No edit button here: it belongs to whichever turn ends up last, which
    // updateEditButton decides once the run settles.
  }
  if (before && before.parentNode === transcriptEl) {
    transcriptEl.insertBefore(node, before);
  } else {
    transcriptEl.appendChild(node);
  }
  scrollDown();
  return node;
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
  const elapsed = ((end - start) / 1000).toFixed(1);
  const label = store.reasoningNode.querySelector('.reasoning-label');
  if (label) label.textContent = t('chat.thought', { s: elapsed });
}

// ---- Tool cards ----------------------------------------------------------
// Tools whose `path` argument points into the agent's workspace — their cards
// offer "open in the Files panel", and read_file results highlight by
// extension.
const PATH_TOOLS = new Set(['read_file', 'write_file', 'edit_file']);
const RESULT_HL_MAX = 200_000; // chars — hljs over megabyte dumps janks the tab
const RESULT_EXPANDABLE_LINES = 12; // roughly what the capped height shows

function toolPath(args) {
  try {
    const p = JSON.parse(args || '{}').path;
    return typeof p === 'string' && p ? p : null;
  } catch {
    return null;
  }
}

function toolActionBtn(iconName, title) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'tool-action';
  b.title = title;
  b.setAttribute('aria-label', title);
  b.innerHTML = icon(iconName, { size: 12 });
  return b;
}

function buildToolNode(call) {
  const node = cloneTemplate('tmpl-tool');
  node.querySelector('.tool-name').textContent = call.name;
  node.querySelector('.tool-args').textContent = formatArgs(call.arguments);
  fillParams(node.querySelector('.tool-params'), call.arguments);
  // The result renderer only needs the name and the path — stash those
  // instead of the full arguments (write_file args can be megabytes).
  node.dataset.toolName = call.name;
  // Identity, so a card already on screen can be recognised rather than drawn
  // twice — see appendTool.
  if (call.id) node.dataset.callId = call.id;
  const path = toolPath(call.arguments);
  if (path && PATH_TOOLS.has(call.name)) {
    node.dataset.toolPath = path;
    const open = toolActionBtn('folder', t('tool.openInFiles'));
    open.classList.add('tool-open-file');
    open.addEventListener('click', (e) => {
      // Inside <summary>: don't let the click also toggle the card.
      e.preventDefault();
      e.stopPropagation();
      store.emit('open-workspace-file', { path });
    });
    node.querySelector('summary')?.appendChild(open);
  }
  return node;
}

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
  if (!existing) appendBubbleContent(store.bubble, node);
  store.toolNodes.set(call.id, node);
  store.body = null;
  store.rawText = '';
  scrollDown();
}

/**
 * The tool card for `id` anywhere in the transcript — the live registry first,
 * then the DOM, which is where a card replayed by the history render lives.
 * @param {string} id Tool call id.
 * @returns {HTMLElement | null}
 */
function findToolNode(id) {
  const known = store.toolNodes.get(id);
  if (known?.isConnected) return known;
  const escaped = window.CSS?.escape ? CSS.escape(id) : id.replace(/["\\]/g, '\\$&');
  return /** @type {HTMLElement | null} */ (
    document.querySelector(`#transcript .tool[data-call-id="${escaped}"]`)
  );
}

// Language for a highlighted result: read_file content only — shell output is
// mixed text where wrong highlighting is worse than none.
function resultLang(node) {
  if (node.dataset.toolName !== 'read_file') return null;
  const ext = (node.dataset.toolPath?.split('.').pop() || '').toLowerCase();
  return /^[a-z0-9]{1,8}$/.test(ext) ? ext : null;
}

// The one renderer behind both the live tool_result event and history replay:
// content (highlighted or linkified), error styling, and the hover actions
// (copy, expand when clipped).
function setToolResult(node, result, isError) {
  const pre = node.querySelector('.tool-result');
  if (!pre) return;
  const text = String(result ?? '');
  if (!text.trim()) {
    pre.style.display = 'none';
    return;
  }
  const lang = !isError && text.length <= RESULT_HL_MAX ? resultLang(node) : null;
  if (lang) {
    const code = document.createElement('code');
    code.className = `language-${lang}`;
    code.textContent = text;
    pre.replaceChildren(code);
    highlightIn(node);
  } else {
    pre.innerHTML = linkifyText(text);
  }
  if (isError) node.classList.add('error');

  const box = node.querySelector('.tool-result-box');
  if (!box) return;
  let actions = box.querySelector('.tool-result-actions');
  if (!actions) {
    actions = document.createElement('div');
    actions.className = 'tool-result-actions';
    box.prepend(actions);
  }
  actions.replaceChildren(); // idempotent across repeated result updates

  if (text.length > 1500 || text.split('\n').length > RESULT_EXPANDABLE_LINES) {
    const expand = toolActionBtn('maximize-2', t('tool.expand'));
    expand.addEventListener('click', () => {
      const on = pre.classList.toggle('expanded');
      expand.title = on ? t('tool.collapse') : t('tool.expand');
    });
    actions.append(expand);
  }
  const copy = toolActionBtn('copy', t('tool.copyResult'));
  copy.addEventListener('click', async () => {
    if (await copyToClipboard(text)) {
      copy.innerHTML = icon('check', { size: 12 });
      setTimeout(() => {
        copy.innerHTML = icon('copy', { size: 12 });
      }, 1500);
    }
  });
  actions.append(copy);
}

function updateToolResult(id, result, isError) {
  const node = store.toolNodes.get(id);
  if (!node) return;
  setToolResult(node, result, isError);
}

function removeToolNode(id) {
  const node = store.toolNodes.get(id);
  if (node) { node.remove(); store.toolNodes.delete(id); }
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
  if (fill == null) {
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

function appendRetry() {
  if (!store.bubble) return;
  const node = cloneTemplate('tmpl-retry');
  const btn = node.querySelector('.retry-btn');
  btn.textContent = t('chat.retry');
  btn.addEventListener('click', () => store.emit('retry'));
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
/** @type {Array<[RegExp, string]>} */
const ERROR_HINTS = [
  // Before the provider-auth pattern: the server's own 401 mentions "server
  // token" precisely so it doesn't read as an API-key problem.
  [/server token/i, t('err.serverToken')],
  [/rate.?limit|too many requests|\b429\b/i, t('err.rateLimit')],
  [/unauthorized|forbidden|api.?key|authenticat|\b401\b|\b403\b/i, t('err.auth')],
  [/quota|billing|insufficient|credit/i, t('err.quota')],
  [/overloaded|service unavailable|\b529\b|\b503\b/i, t('err.overloaded')],
  [/timed?.?out|timeout/i, t('err.timeout')],
  [/failed to fetch|networkerror|load failed|fetch failed/i, t('err.network')],
];

// The friendly sentence for a raw error, or null when it's unrecognized
// (callers then show the raw message alone).
function humanizeError(message) {
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
function appendErrorNotice(message) {
  if (!store.bubble) return;
  const note = document.createElement('div');
  note.className = 'error-notice';
  const hint = humanizeError(message);
  if (hint) {
    const head = document.createElement('div');
    head.textContent = `⚠️ ${hint}`;
    const detail = document.createElement('div');
    detail.className = 'error-notice-detail';
    detail.textContent = String(message);
    note.append(head, detail);
  } else {
    note.textContent = `⚠️ ${message}`;
  }
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

function addCopyButton(bubble) {
  if (!bubble) return;
  for (const node of bubble.querySelectorAll(':scope > .btn-copy, :scope > .turn-footer > .btn-copy')) {
    node.remove();
  }
  const bodies = Array.from(bubble.children).filter((node) =>
    node.classList?.contains('body')
  );
  const footer = ensureFooter(bubble);
  const markdown = cleanMarkdownForCopy(bodies
    .map((body) => body.dataset.raw || body.textContent || '')
    .map((text) => text.trim())
    .filter(Boolean)
    .join('\n\n'));
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

// ---- Mid-run injection: queued user turns awaiting confirmation ----------
let _queuedTurns = [];   // FIFO of muted user-turn nodes (one per pending inject)
let _pendingResend = []; // { message, node } pairs that raced a run's end (see runStream)

// True when an assistant turn carries no rendered content yet (just opened, or
// only an empty footer). Avoids stranding an empty bubble between two messages
// injected at the same turn boundary.
function assistantTurnIsEmpty(node) {
  const bubble = node?.querySelector('.bubble');
  if (!bubble) return true;
  for (const child of bubble.children) {
    if (!child.classList.contains('turn-footer')) return false;
  }
  return true;
}

// Finalize the current (streaming) assistant turn: stop its spinner, stamp it,
// add a copy button, and reset the live-render pointers so the next turn opens
// clean. Shared by the run-end paths and the injection bubble rotation.
function finalizeCurrentAssistantTurn() {
  finalizeReasoning();
  const node = store.turnNode;
  if (node) {
    if (assistantTurnIsEmpty(node)) {
      // e.g. an attach opened a tail bubble the run never wrote into —
      // don't leave an empty grey bubble in the transcript.
      node.remove();
    } else {
      node.classList.remove('streaming');
      setTurnTimestamp(node);
      addCopyButton(store.bubble);
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
  addCopyButton(node.querySelector('.bubble'));
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
  if (!_pendingResend.length || !store.sessionId) return;
  const items = _pendingResend.splice(0);
  for (const it of items) {
    let res = null;
    try {
      res = await api.inject({ session_id: store.sessionId, message: it.message });
    } catch { /* network error — treat as raced again */ }
    if (res?.accepted) {
      // A snapshot re-render may have wiped the muted bubble — recreate it.
      if (!it.node?.isConnected) it.node = appendUserTurn(it.message, { queued: true });
      if (it.node) {
        _queuedTurns.push(it.node);
        addWithdrawButton(it.node, res.id);
      }
    } else {
      _pendingResend.push(it); // still no live run — the next finally retries
    }
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

function loadEarlierHistory() {
  const el = document.getElementById('transcript');
  if (!el) return;
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
      addCopyButton(bubble);
      transcriptEl.appendChild(turn);
    } else if (it.role === 'assistant') {
      if (!currentBubble) {
        if (it.timestamp) {
          maybeDateSeparator(transcriptEl, it.timestamp, { anchorFirst: true });
        }
        const result = startAssistantTurn(it.timestamp);
        currentBubble = result.bubble;
      }
      const text = contentText(it.content);
      if (it.reasoning) {
        const details = document.createElement('details');
        details.className = 'reasoning done';
        const summary = document.createElement('summary');
        summary.innerHTML = `<span class="reasoning-icon">💭</span><span class="reasoning-label">${t('chat.thinking')}</span>`;
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
        body.innerHTML = renderMarkdown(text);
        appendBubbleContent(currentBubble, body);
        highlightCode(body);
        renderMermaid(body);
      }
      if (it.tool_calls) {
        for (const call of it.tool_calls) {
          // Replayed history counts too: "touched" means files THIS chat
          // produced, whether live or reloaded.
          emitWorkspaceTouch(call.name, call.arguments);
          const todos = parseTodos(call.arguments);
          if (todos) {
            upsertTodoCard(todos); // render/update the session's checklist panel
            continue;
          }
          const node = buildToolNode(call);
          const result = pendingResults.get(call.id);
          // Same renderer as the live path: highlighting, error styling, and
          // the copy/expand actions all match; absent results hide the <pre>.
          setToolResult(node, result?.text ?? '', result?.isError ?? false);
          appendBubbleContent(currentBubble, node);
        }
      }
      addCopyButton(currentBubble);
    } else if (it.role === 'context_compacted') {
      // Persisted run-boundary notice — render into the run's bubble (matching
      // the live placement), or the transcript if the run had no assistant turn.
      appendContextCompacted(currentBubble || transcriptEl, it.compaction);
    }
  }

  // Remove streaming markers
  transcriptEl.querySelectorAll('.turn.streaming').forEach(n => n.classList.remove('streaming'));
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

/** Render the empty-state (welcome) view — title, description, example prompts — into the transcript. */
export function renderEmptyState() {
  const transcript = document.getElementById('transcript');
  if (!transcript) return;
  const title = store.emptyTitle || 'Where shall we begin?';
  const desc = store.emptyDescription;
  const empty = document.createElement('div');
  empty.className = 'empty-state';
  empty.id = 'empty-state';
  const h2 = document.createElement('h2');
  h2.textContent = title;
  empty.appendChild(h2);
  if (Array.isArray(desc)) {
    const ul = document.createElement('ul');
    for (const item of desc) {
      const li = document.createElement('li');
      li.textContent = item;
      ul.appendChild(li);
    }
    empty.appendChild(ul);
  } else if (desc) {
    const p = document.createElement('p');
    p.textContent = desc;
    empty.appendChild(p);
  }
  if (store.emptyExamples?.length) {
    const wrap = document.createElement('div');
    wrap.className = 'empty-examples';
    for (const example of store.emptyExamples) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip empty-example';
      btn.textContent = example;
      wrap.appendChild(btn);
    }
    empty.appendChild(wrap);
  }
  transcript.replaceChildren(empty);
}

// ---- SSE ---------------------------------------------------------------
const turnProgressEl = document.getElementById('turn-progress');
let _currentTurn = 0; // the pill shows "Turn N · <current tool>…"

// True while the most recent SSE event was `error`. If the stream then ends
// (no `done`), the failure was terminal — offer a Retry; a mid-run tool error
// is followed by more events, which reset this.
let _lastEventWasError = false;

// True once this stream produced at least one `done` (an auto-chaining run
// emits one per leg). Read in the stream's finally to tell "the run answered"
// from "cancelled or died", which is what gates the follow-up chips.
let _sawDone = false;

// This stream's last assistant text, for the follow-up cache signature. It
// can't be read off store.rawText in the finally: `message_completed` clears
// that as each model turn lands, long before `done` — so the finally always
// saw '' and every cache write was silently skipped. Taken from the event
// payload instead, which is the same joined turn text the server persists
// (sse.py `_entries_to_dict` / transcript.py `entries_to_messages`), so this
// signature matches the one `tailSig` recomputes from reloaded history.
let _lastReply = '';

async function handleEvent({ event, data }) {
  _lastEventWasError = event === 'error';
  switch (event) {
    case 'session': {
      const known = store.sessions.some((s) => s.id === data.session_id);
      store.sessionId = data.session_id;
      store.syncURL(data.session_id);
      // A brand-new session gets a server-generated title shortly after the
      // first turn; flag it so the stream's end polls for it (see pollForTitle).
      store.titlePending = !known;
      // Surface it in the sidebar right away — with its provisional title —
      // instead of waiting for the run to finish.
      if (!known) loadSessions();
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
      clearTimeout(_renderTimer);
      if (store.body) { store.body.remove(); store.body = null; }
      store.rawText = '';
      if (store.reasoningNode) { store.reasoningNode.remove(); store.reasoningNode = null; }
      store.reasoningText = '';
      store.reasoningStart = 0;
      store.reasoningEnd = 0;
      break;

    case 'message_completed':
      clearTimeout(_renderTimer);
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
        confirmQueuedTurn(queued);
      } else {
        // Injected elsewhere (e.g. another tab): render above the fresh bubble.
        appendUserTurn(data.content, { before: store.turnNode });
      }
      scrollDown();
      break;
    }

    case 'tool_call':
      emitWorkspaceTouch(data.name, data.arguments);
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
      // The pill says what the run is doing, not just how far it is.
      if (turnProgressEl && _currentTurn) {
        turnProgressEl.textContent = `${t('topbar.turn', { n: _currentTurn })} · ${data.name}…`;
      }
      break;

    case 'tool_result':
      // A finished shell command may have created/edited files we can't see
      // individually — let the Files panel mark its listing as maybe stale.
      if (data.name === 'shell') store.emit('workspace-maybe-stale');
      updateToolResult(data.id, data.result, data.is_error);
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

    case 'turn_started':
      _currentTurn = data.turn;
      if (turnProgressEl) {
        turnProgressEl.textContent = t('topbar.turn', { n: data.turn });
        turnProgressEl.classList.remove('hidden');
      }
      break;

    case 'context_compacted':
      appendContextCompacted(store.bubble, data);
      break;

    case 'error':
      appendErrorNotice(data.message);
      break;

    case 'done': {
      _sawDone = true;
      _dropRecoveries = 0; // the connection proved healthy — re-arm recovery
      if (turnProgressEl) turnProgressEl.classList.add('hidden');
      updateContextMeter(data?.usage);
      // Stamp the run's token spend into the turn footer — the data is
      // already on the wire; hovering shows the input/output split.
      const tokens = formatTokens(data?.usage?.total_tokens);
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
  await loadSessions(); // ensure the provisional title is on screen first
  if (store.sessionId !== sessionId) return; // user moved on
  const provisional = store.sessions.find((s) => s.id === sessionId)?.title ?? null;
  let attempt = 0;

  // Keep the timer callback synchronous and swallow rejections so a failed
  // poll can never surface as an unhandled promise rejection.
  const schedule = (ms) => {
    _titlePollTimer = setTimeout(() => void tick().catch(() => {}), ms);
  };

  async function tick() {
    _titlePollTimer = null;
    if (store.sessionId !== sessionId) return;
    await loadSessions();
    const current = store.sessions.find((s) => s.id === sessionId)?.title ?? null;
    const landed = current && current !== provisional;
    if (!landed && attempt < _TITLE_POLL_BACKOFF_MS.length) {
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

// While a run streams, Stop takes the Send circle's place; Send comes back —
// as a queue control — the moment there's text to queue (updateSendEnabled),
// and pressing Enter queues it for the next turn/run. The placeholder is what
// makes queuing discoverable.
function enterStreamingUI() {
  store.streaming = true;
  syncAttachButton();
  document.getElementById('reconnect-row')?.remove(); // superseded by a live stream
  if (stopBtn) {
    stopBtn.style.display = '';
    stopBtn.title = t('composer.stopTip');
  }
  if (promptEl) promptEl.placeholder = t('composer.queuePlaceholder');
  updateSendEnabled();
  // Keep screen readers from re-announcing every 60 ms streaming re-render;
  // they pick the transcript back up once the turn settles.
  document.getElementById('transcript')?.setAttribute('aria-busy', 'true');
  updateRegenButton(); // streaming: no regen affordance until the run settles
  updateEditButton(); // …and no edit either: rewind is refused during a live run
}

function exitStreamingUI() {
  store.streaming = false;
  syncAttachButton();
  if (stopBtn) stopBtn.style.display = 'none';
  if (promptEl) {
    promptEl.placeholder = t('composer.placeholder');
    promptEl.focus();
  }
  updateSendEnabled();
  document.getElementById('transcript')?.setAttribute('aria-busy', 'false');
  updateRegenButton();
  updateEditButton();
}

/**
 * Send `message` (with optional attachments) and stream the assistant's reply
 * into a new turn.
 * @param {string} message
 * @param {any[] | null} [attachments] Prepared attachment descriptors.
 * @returns {Promise<void>}
 */
export async function runStream(message, attachments = null) {
  store.lastMessage = message;
  store.titlePending = false; // set true by the `session` event for new chats
  stopTitlePolling();
  clearFollowups(); // the previous turn's suggestions are answered or abandoned
  _dropRecoveries = 0; // a fresh user action re-arms auto-recovery
  enterStreamingUI();
  startAssistantTurn();
  const streamEpoch = store.chatEpoch;
  _lastEventWasError = false;
  _sawDone = false;
  _lastReply = '';
  // Set on every path that already surfaced a failure (or a deliberate abort),
  // so the finally's dropped-connection check doesn't fire on top of it.
  let terminal = false;

  _resumeAutoScroll();
  _streamAbortController = new AbortController();

  try {
    const res = await api.streamChat(
      {
        message,
        agent: store.agent,
        session_id: store.sessionId,
        attachments: attachments?.length ? attachments : undefined,
      },
      { signal: _streamAbortController.signal }
    );

    if (!res.ok || !res.body) {
      terminal = true;
      // Prefer the server's {detail} (e.g. "too many concurrent runs") over
      // a bare status line.
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body?.detail) detail = String(body.detail);
      } catch { /* not JSON */ }
      const hint = humanizeError(detail);
      ensureBody().innerHTML =
        `<span class="error-text">${t('chat.error')}: ${escapeHtml(hint ?? detail)}</span>` +
        (hint ? `<div class="error-notice-detail">${escapeHtml(detail)}</div>` : '');
      appendRetry();
      return;
    }

    // The run is registered — messages that raced the previous run's end can
    // re-enter the queue now (each as its own turn).
    drainPendingResend();

    for await (const ev of readSSE(res)) {
      if (store.chatEpoch !== streamEpoch) {
        _streamAbortController?.abort();
        return;
      }
      await handleEvent(ev);
    }
  } catch (err) {
    // A session switch / new chat detaches by bumping the epoch and aborting
    // the fetch — not an error, and the view we left is gone. Leave it alone.
    if (store.chatEpoch !== streamEpoch) return;
    terminal = true;
    if (err.name === 'AbortError') {
      ensureBody();
      if (!store.rawText) store.rawText = t('chat.cancelled');
      flushRender(true);
    } else {
      ensureBody();
      const raw = err.message ?? String(err);
      const hint = humanizeError(raw);
      store.rawText += `\n\n> ⚠️ **${t('chat.error')}:** ${hint ? `${hint} (${raw})` : raw}`;
      flushRender(true);
      appendRetry();
    }
  } finally {
    clearTimeout(_renderTimer);
    // If a switch superseded this stream, its DOM/UI now belong to another view;
    // do only the connection-local cleanup above and skip the rest.
    if (store.chatEpoch === streamEpoch) {
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
      exitStreamingUI();
      _streamAbortController = null;
      if (turnProgressEl) turnProgressEl.classList.add('hidden');
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
  const streamEpoch = store.chatEpoch;
  _lastEventWasError = false;
  _sawDone = false;
  _lastReply = '';
  let terminal = false; // see runStream — gates the dropped-connection check
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

    drainPendingResend(); // same re-queue point as runStream

    for await (const ev of readSSE(res)) {
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
    if (err.name !== 'AbortError') {
      ensureBody();
      const raw = err.message ?? String(err);
      const hint = humanizeError(raw);
      store.rawText += `\n\n> ⚠️ **${t('chat.error')}:** ${hint ? `${hint} (${raw})` : raw}`;
      flushRender(true);
    }
  } finally {
    clearTimeout(_renderTimer);
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
      exitStreamingUI();
      _streamAbortController = null;
      if (turnProgressEl) turnProgressEl.classList.add('hidden');
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
  clearTimeout(_renderTimer);
  // The superseded run's finally is now epoch-guarded off, so any *global* state
  // it would have reset has to be reset here or it leaks into the next view:
  //  - queued/pending buffers, else a later run's finally flushes stale bubbles
  //    or resends a raced message into the wrong chat (accepted injects still
  //    replay from the server mailbox on reconnect; an un-accepted one is dropped);
  //  - the turn-progress pill (un-hidden by turn_started);
  //  - the todo panel (only renderHistory rebuilds it — a failed switch won't);
  //  - any in-flight follow-up request, whose chips would land in the new view.
  _queuedTurns = [];
  _pendingResend = [];
  if (turnProgressEl) turnProgressEl.classList.add('hidden');
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
// switch as the Files panel — and disabled mid-stream (attachments need a
// fresh turn, not the inject/queue path).
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

function updateSendEnabled() {
  if (!sendBtn) return;
  const hasText = !!promptEl?.value.trim();
  if (store.streaming) {
    // Stop owns the slot; Send reappears only once there's text to queue —
    // a greyed Send next to Stop reads as two competing controls.
    sendBtn.hidden = !hasText;
    sendBtn.disabled = !hasText;
    sendBtn.title = t('composer.queueTip');
    sendBtn.setAttribute('aria-label', t('composer.queueSend'));
  } else {
    sendBtn.hidden = false;
    sendBtn.disabled = !(hasText || pendingReady().length);
    sendBtn.title = t('composer.sendTip');
    sendBtn.setAttribute('aria-label', t('composer.send'));
  }
}

function syncAttachButton() {
  const btn = /** @type {HTMLButtonElement | null} */ (document.getElementById('attach'));
  if (!btn) return;
  const ok = attachEnabled();
  btn.hidden = !ok;
  btn.disabled = !ok || store.streaming;
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
    if (!store.streaming && attachEnabled()) attachInput.click();
  });
  promptEl?.addEventListener('paste', (e) => {
    if (store.streaming || !attachEnabled()) return;
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
      if (store.streaming || !attachEnabled()) return;
      if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
      e.preventDefault();
      composer.classList.add('drag-over');
    });
    composer.addEventListener('dragleave', (e) => {
      if (!composer.contains(/** @type {Node} */ (e.relatedTarget))) composer.classList.remove('drag-over');
    });
    composer.addEventListener('drop', (e) => {
      composer.classList.remove('drag-over');
      if (store.streaming || !attachEnabled()) return;
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
    if (store.streaming) {
      toast(t('files.attachBusy'), { type: 'error' });
      return;
    }
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
    if (!btn || !promptEl) return;
    promptEl.value = btn.textContent;
    autoresize();
    updateSendEnabled();
    saveDraftSoon();
    promptEl.focus();
  });

  composer?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const message = promptEl.value.trim();

    if (store.streaming) {
      // Queue path — text only; attachments need a fresh turn, not inject.
      if (!message) return;
      promptEl.value = '';
      autoresize();
      updateSendEnabled();
      clearDraft();
      _resumeAutoScroll();
      // Queue it: the server drains it at the next turn start, or seeds the
      // next run if this one ends first. Show a muted bubble (with a cancel
      // affordance) until the run confirms it or the user withdraws it.
      const node = appendUserTurn(message, { queued: true });
      if (node) _queuedTurns.push(node);
      let res = null;
      try {
        res = await api.inject({ session_id: store.sessionId, message });
      } catch { /* network error → treat as no active run */ }
      if (res?.accepted) {
        if (node) addWithdrawButton(node, res.id);
      } else {
        // Raced the run's end: keep the bubble muted and deliver the message
        // as its own turn once the stream settles — the next run re-injects it
        // (drainPendingResend) or it seeds a fresh run (resumePendingResend).
        const i = _queuedTurns.indexOf(node);
        if (i >= 0) _queuedTurns.splice(i, 1);
        _pendingResend.push({ message, node });
      }
      return;
    }

    const attachments = pendingReady();
    if (!message && !attachments.length) return;
    const payload = attachments.map((a) => ({
      path: a.path,
      mime: a.mime,
      kind: a.kind,
      name: a.name,
    }));
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
}
