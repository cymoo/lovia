// Chat streaming, SSE handling, message rendering.
import { store } from './store.js';
import { api, readSSE } from './api.js';
import { copyToClipboard } from './ui.js';
import { loadSessions } from './sessions.js';
import { renderMermaid } from './diagrams.js';
import { icon } from './icons.js';

// ---- Markdown & Highlighting -------------------------------------------
marked.setOptions({ gfm: true, breaks: false });

function renderMarkdown(text) {
  if (!text.trim()) return '';
  const raw = marked.parse(text);
  return typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(raw) : raw;
}

function highlightCode(container) {
  if (typeof hljs === 'undefined') return;
  container.querySelectorAll('pre code').forEach((el) => {
    if (el.classList.contains('language-mermaid')) return; // rendered as a diagram instead
    if (!el.dataset.highlighted) {
      hljs.highlightElement(el);
      el.dataset.highlighted = '1';
    }
  });
  addCodeBlockControls(container);
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
    btn.title = 'Copy code';
    btn.innerHTML = `${icon('copy', { size: 12 })} Copy`;
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const codeText = code?.textContent || pre.textContent?.replace(/Copy$/, '') || '';
      const ok = await copyToClipboard(codeText.trimEnd());
      if (ok) {
        btn.innerHTML = `${icon('check', { size: 12 })} Copied!`;
        btn.classList.add('copied');
        setTimeout(() => {
          btn.innerHTML = `${icon('copy', { size: 12 })} Copy`;
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
function flushRender() {
  if (!store.body || !store.rawText) return;
  store.body.dataset.raw = store.rawText;
  store.body.innerHTML = renderMarkdown(store.rawText);
  highlightCode(store.body);
  renderMermaid(store.body);
  scrollDown();
}

// ---- Templates ---------------------------------------------------------
function cloneTemplate(id) {
  return document.getElementById(id).content.firstElementChild.cloneNode(true);
}

function makeTurn(role, ts) {
  const node = cloneTemplate('tmpl-turn');
  node.classList.add(role);
  setTurnTimestamp(node, ts);
  return node;
}

function setTurnTimestamp(turn, ts = Date.now()) {
  if (!turn) return;
  turn.dataset.timestamp = formatTimestamp(ts);
  const timestamp = turn.querySelector('.turn-footer .timestamp');
  if (timestamp) timestamp.textContent = turn.dataset.timestamp;
}

function formatTimestamp(ts) {
  // Accept seconds (float from backend) or ms.
  if (ts == null) ts = Date.now();
  const ms = ts > 1e12 ? ts : ts * 1000;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) d.setTime(Date.now());
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function formatArgs(args) {
  if (!args) return '()';
  try {
    const obj = JSON.parse(args);
    const entries = Object.entries(obj);
    if (entries.length === 0) return '()';
    return '(' + entries.map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join(', ') + ')';
  } catch {
    return `(${args})`;
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

// ---- Render helpers ----------------------------------------------------
export function appendUserTurn(text, { queued = false, before = null } = {}) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return null;
  const node = makeTurn('user');
  if (queued) node.classList.add('queued');
  const body = document.createElement('div');
  body.className = 'body';
  body.textContent = text;
  const bubble = node.querySelector('.bubble');
  appendBubbleContent(bubble, body);
  ensureFooter(bubble);
  if (!queued) addCopyButton(bubble); // queued bubbles get their copy button on confirm
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
    summary.innerHTML = '<span class="reasoning-icon">💭</span><span class="reasoning-label">Thinking…</span>';
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
  if (label) label.textContent = `Thought for ${elapsed}s`;
}

function buildToolNode(call) {
  const node = cloneTemplate('tmpl-tool');
  node.querySelector('.tool-name').textContent = call.name;
  node.querySelector('.tool-args').textContent = formatArgs(call.arguments);
  return node;
}

function appendTool(call) {
  if (!store.bubble) return;
  const node = buildToolNode(call);
  appendBubbleContent(store.bubble, node);
  store.toolNodes.set(call.id, node);
  store.body = null;
  store.rawText = '';
  scrollDown();
}

function updateToolResult(id, result, isError) {
  const node = store.toolNodes.get(id);
  if (!node) return;
  const pre = node.querySelector('.tool-result');
  if (!result || !String(result).trim()) {
    if (pre) pre.style.display = 'none';
    return;
  }
  if (pre) pre.textContent = String(result);
  if (isError) node.classList.add('error');
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
      return obj.todos.map((t) => ({
        content: t.content ?? '',
        status: t.status ?? 'pending',
        active_form: t.active_form ?? null,
      }));
    }
  } catch { /* not a todo payload */ }
  return null;
}

function fillTodoCard(card, todos) {
  const total = todos.length;
  const done = todos.filter((t) => t.status === 'completed').length;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const expanded = !store.todoCollapsed;
  card.classList.toggle('complete', total > 0 && done === total);
  card.innerHTML =
    `<button class="todo-toggle" type="button" aria-expanded="${expanded}" title="${expanded ? 'Hide plan' : 'Show plan'}">` +
    '<span class="todo-title">Plan</span>' +
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
  for (const t of todos) {
    const status = ['pending', 'in_progress', 'completed'].includes(t.status) ? t.status : 'pending';
    const li = document.createElement('li');
    li.className = `todo-item ${status}`;
    const label = status === 'in_progress' && t.active_form ? t.active_form : t.content;
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
  const toggle = panel?.querySelector('.todo-toggle');
  const icon = panel?.querySelector('.todo-toggle-icon');
  toggle?.setAttribute('aria-expanded', String(!collapsed));
  if (toggle) toggle.title = collapsed ? 'Show plan' : 'Hide plan';
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
  store.todoCollapsed = false;
  store.todos = [];
}

function resetChatView() {
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

function appendApproval(call) {
  if (!store.bubble) return;
  const node = cloneTemplate('tmpl-approval');
  node.querySelector('.approval-name').textContent = call.name;
  node.querySelector('.approval-args').textContent = formatArgs(call.arguments);
  const resolve = async (decision) => {
    node.classList.add('resolved');
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
}

function appendHandoff(from, to) {
  if (!store.bubble) return;
  const node = cloneTemplate('tmpl-handoff');
  node.querySelector('.handoff-text').textContent = `${from}  →  ${to}`;
  appendBubbleContent(store.bubble, node);
}

function appendContextCompacted(data) {
  if (!store.bubble) return;
  const node = cloneTemplate('tmpl-context-compacted');
  const msg = data.summary ? 'Context compacted with summary.' : 'Context compacted.';
  node.querySelector('.context-text').textContent = msg;
  appendBubbleContent(store.bubble, node);
}

function appendRetry() {
  if (!store.bubble) return;
  const node = cloneTemplate('tmpl-retry');
  node.querySelector('.retry-btn').addEventListener('click', () => store.emit('retry'));
  appendBubbleContent(store.bubble, node);
}

// A run-level error that the run itself recovers from — most commonly a tool
// raising, which lovia feeds back to the model to handle. Show it as a quiet
// inline notice (no Retry: re-sending the whole turn doesn't retry the tool,
// and the model usually copes on its own).
function appendErrorNotice(message) {
  if (!store.bubble) return;
  const note = document.createElement('div');
  note.className = 'error-notice';
  note.textContent = `⚠️ ${message}`;
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
let _pendingResend = []; // messages that raced a run's end; resent next (see runStream)

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
    node.classList.remove('streaming');
    setTurnTimestamp(node);
    addCopyButton(store.bubble);
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
  addCopyButton(node.querySelector('.bubble'));
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
  btn.title = 'Cancel this queued message';
  btn.setAttribute('aria-label', 'Cancel queued message');
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

// ---- History rendering --------------------------------------------------
export function renderHistory(entries) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return;
  // Swapping the transcript collapses scrollHeight and snaps scrollTop to 0,
  // firing a 'scroll' event the handler would misread as the user scrolling up
  // — which disables sticky-bottom. Guard the swap exactly like scrollDown()
  // does: the reset's (coalesced) scroll event fires before the rAF below
  // releases the flag, so it's ignored. Without this, switching back to a
  // still-streaming chat rendered its snapshot and then never re-pinned to the
  // live tail.
  _programmaticScroll = true;
  transcriptEl.innerHTML = '';
  _resumeAutoScroll();
  resetChatView();
  store.bubble = null;
  store.body = null;
  store.rawText = '';
  store.toolNodes.clear();

  const pendingResults = new Map();
  for (const it of entries) {
    // History entries are MessageOut (role + tool_call_id), with no `type`
    // field — gating on `it.type` here left every result unmatched and hidden.
    if (it.role === 'tool' && it.tool_call_id)
      pendingResults.set(it.tool_call_id, contentText(it.content));
  }

  let currentBubble = null;
  for (const it of entries) {
    if (it.role === 'user') {
      currentBubble = null;
      const turn = makeTurn('user', it.timestamp);
      const body = document.createElement('div');
      body.className = 'body';
      body.textContent = contentText(it.content);
      const bubble = turn.querySelector('.bubble');
      appendBubbleContent(bubble, body);
      ensureFooter(bubble);
      addCopyButton(bubble);
      transcriptEl.appendChild(turn);
    } else if (it.role === 'assistant') {
      if (!currentBubble) {
        const result = startAssistantTurn(it.timestamp);
        currentBubble = result.bubble;
      }
      const text = contentText(it.content);
      if (it.reasoning) {
        const details = document.createElement('details');
        details.className = 'reasoning done';
        const summary = document.createElement('summary');
        summary.innerHTML = '<span class="reasoning-icon">💭</span><span class="reasoning-label">Thinking</span>';
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
          const todos = parseTodos(call.arguments);
          if (todos) {
            upsertTodoCard(todos); // render/update the session's checklist panel
            continue;
          }
          const node = buildToolNode(call);
          const result = pendingResults.get(call.id);
          if (result !== undefined && result !== '') {
            node.querySelector('.tool-result').textContent = result;
          } else {
            // No result stored — hide the empty <pre>
            const pre = node.querySelector('.tool-result');
            if (pre) pre.style.display = 'none';
          }
          appendBubbleContent(currentBubble, node);
        }
      }
      addCopyButton(currentBubble);
    }
  }

  // Remove streaming markers
  transcriptEl.querySelectorAll('.turn.streaming').forEach(n => n.classList.remove('streaming'));
  store.bubble = null;
  store.body = null;
  store.rawText = '';
  // Land at the bottom now (content is static at this point) and record it as
  // _lastScrollTop so the swap's async scroll event reads as "no movement";
  // then release the guard next frame. Live deltas re-pin via scrollDown().
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
  _lastScrollTop = transcriptEl.scrollTop;
  requestAnimationFrame(() => { _programmaticScroll = false; });
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

export function renderEmptyState() {
  const transcript = document.getElementById('transcript');
  if (!transcript) return;
  const title = store.emptyTitle || 'Wake up, Neo.';
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
  transcript.replaceChildren(empty);
}

// ---- SSE ---------------------------------------------------------------
const turnProgressEl = document.getElementById('turn-progress');

async function handleEvent({ event, data }) {
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
      // history-so-far, then open a bubble for the live tail that follows.
      renderHistory(data.entries || []);
      startAssistantTurn();
      break;

    case 'text_delta':
      finalizeReasoning();
      ensureBody();
      store.rawText += data.delta;
      scheduleRender();
      break;

    case 'reasoning_delta':
      ensureReasoning();
      if (!store.reasoningStart) store.reasoningStart = Date.now();
      store.reasoningEnd = Date.now();
      store.reasoningText += data.delta;
      if (store.reasoningNode)
        store.reasoningNode.querySelector('.reasoning-content').textContent = store.reasoningText;
      scrollDown();
      break;

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
      if (store.body && store.rawText) flushRender();
      store.body = null;
      store.rawText = '';
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
      break;

    case 'tool_result':
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
      if (turnProgressEl) {
        turnProgressEl.textContent = `Turn ${data.turn}`;
        turnProgressEl.classList.remove('hidden');
      }
      break;

    case 'context_compacted':
      appendContextCompacted(data);
      break;

    case 'error':
      appendErrorNotice(data.message);
      break;

    case 'done':
      if (turnProgressEl) turnProgressEl.classList.add('hidden');
      break;
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

// ---- Streaming ---------------------------------------------------------
const sendBtn = document.getElementById('send');
const stopBtn = document.getElementById('stop');
const composer = document.getElementById('composer');
const promptEl = document.getElementById('prompt');
let _streamAbortController = null;

// While a run streams, the composer stays usable: Send becomes "Queue" (next
// turn / next run) and Stop appears beside it. Keeping Send visible is what
// makes queuing discoverable.
function enterStreamingUI() {
  store.streaming = true;
  if (stopBtn) stopBtn.style.display = '';
  if (sendBtn) {
    sendBtn.textContent = 'Queue';
    sendBtn.setAttribute('aria-label', 'Queue message');
  }
  if (promptEl) promptEl.placeholder = 'Queue a follow-up…';
}

function exitStreamingUI() {
  store.streaming = false;
  if (stopBtn) stopBtn.style.display = 'none';
  if (sendBtn) {
    sendBtn.textContent = 'Send';
    sendBtn.setAttribute('aria-label', 'Send');
  }
  if (promptEl) {
    promptEl.placeholder = 'Send a message…';
    promptEl.focus();
  }
}

export async function runStream(message) {
  store.lastMessage = message;
  store.titlePending = false; // set true by the `session` event for new chats
  stopTitlePolling();
  enterStreamingUI();
  startAssistantTurn();
  const streamEpoch = store.chatEpoch;

  _resumeAutoScroll();
  _streamAbortController = new AbortController();

  try {
    const res = await api.streamChat(
      { message, agent: store.agent, session_id: store.sessionId },
      { signal: _streamAbortController.signal }
    );

    if (!res.ok || !res.body) {
      ensureBody().innerHTML = `<span class="error-text">Error: ${res.status} ${res.statusText}</span>`;
      appendRetry();
      return;
    }

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
    if (err.name === 'AbortError') {
      ensureBody();
      if (!store.rawText) store.rawText = '_Cancelled._';
      flushRender();
    } else {
      ensureBody();
      store.rawText += `\n\n> ⚠️ **Error:** ${err.message ?? err}`;
      flushRender();
      appendRetry();
    }
  } finally {
    clearTimeout(_renderTimer);
    // If a switch superseded this stream, its DOM/UI now belong to another view;
    // do only the connection-local cleanup above and skip the rest.
    if (store.chatEpoch === streamEpoch) {
      if (store.body && store.rawText) {
        store.body.dataset.raw = store.rawText; // store raw markdown for copy
        flushRender();
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
      // A message that raced this run's end (inject → accepted:false) starts a
      // fresh run now that the stream has fully settled.
      if (_pendingResend.length) {
        runStream(_pendingResend.splice(0).join('\n\n'));
      }
    }
  }
}

export async function runReconnect(sessionId) {
  enterStreamingUI();
  startAssistantTurn();
  const streamEpoch = store.chatEpoch;

  _resumeAutoScroll();
  _streamAbortController = new AbortController();

  try {
    const res = await api.reconnect(sessionId, {
      signal: _streamAbortController.signal,
    });
    if (store.chatEpoch !== streamEpoch) return; // switched away mid-request

    if (!res.ok || !res.body) {
      // 404 = nothing to reconnect, 409 = already running or agent gone.
      // Either way: silently remove the empty placeholder and let the user
      // see the already-rendered history without an error message.
      store.turnNode?.remove();
      store.turnNode = null;
      return;
    }

    for await (const ev of readSSE(res)) {
      if (store.chatEpoch !== streamEpoch) {
        _streamAbortController?.abort();
        return;
      }
      await handleEvent(ev);
    }
  } catch (err) {
    if (store.chatEpoch !== streamEpoch) return; // detached by a switch — not an error
    if (err.name !== 'AbortError') {
      ensureBody();
      store.rawText += `\n\n> ⚠️ **Error:** ${err.message ?? err}`;
      flushRender();
    }
  } finally {
    clearTimeout(_renderTimer);
    // A switch superseded this reconnect: its DOM/UI belong to another view now.
    if (store.chatEpoch === streamEpoch) {
      if (store.body && store.rawText) {
        store.body.dataset.raw = store.rawText;
        flushRender();
      }
      finalizeCurrentAssistantTurn();
      flushQueuedTurns();
      exitStreamingUI();
      _streamAbortController = null;
      if (turnProgressEl) turnProgressEl.classList.add('hidden');
      loadSessions();
      if (_pendingResend.length) {
        runStream(_pendingResend.splice(0).join('\n\n'));
      }
    }
  }
}

export function resetChatForNewSession() {
  detachStream(); // keep any live run going server-side; just disconnect from it
  resetChatView();
  renderEmptyState();
  _resumeAutoScroll(); // after the swap, so the reset's scroll event is a no-op
}

// Detach the client from the in-flight run WITHOUT cancelling it server-side.
// The supervised run keeps streaming and stays reachable (its sidebar dot
// persists), so clicking back into the session reconnects to it. Bumps the
// epoch so the live runStream/runReconnect loop bails and its catch/finally
// no-op — the view we're moving to now owns the DOM — aborts the SSE fetch (the
// server treats the dropped connection as a detach), and returns the composer
// to its idle state. Contrast cancelStream(), which tells the server to stop.
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
  //  - the todo panel (only renderHistory rebuilds it — a failed switch won't).
  _queuedTurns = [];
  _pendingResend = [];
  if (turnProgressEl) turnProgressEl.classList.add('hidden');
  clearTodoPanel();
  if (store.streaming) exitStreamingUI();
}

export async function cancelStream() {
  if (_streamAbortController) _streamAbortController.abort();
  if (store.sessionId) {
    try {
      await api.cancel(store.sessionId);
    } catch { /* ignore */ }
  }
}

// ---- Composer ----------------------------------------------------------
const autoresize = () => {
  if (!promptEl) return;
  promptEl.style.height = 'auto';
  promptEl.style.height = Math.min(promptEl.scrollHeight, window.innerHeight * 0.3) + 'px';
};

export function initComposer() {
  promptEl?.addEventListener('input', autoresize);
  promptEl?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      composer?.requestSubmit();
    }
  });

  composer?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const message = promptEl.value.trim();
    if (!message) return;
    promptEl.value = '';
    autoresize();
    _resumeAutoScroll();

    if (store.streaming) {
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
        // Raced the run's end: confirm the bubble and deliver it as a fresh run
        // once the current stream settles (see runStream's finally).
        const i = _queuedTurns.indexOf(node);
        if (i >= 0) _queuedTurns.splice(i, 1);
        confirmQueuedTurn(node);
        _pendingResend.push(message);
      }
      return;
    }

    document.getElementById('empty-state')?.remove();
    appendUserTurn(message);
    await runStream(message);
  });

  stopBtn?.addEventListener('click', () => cancelStream());
}
