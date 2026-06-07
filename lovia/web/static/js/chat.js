// Chat streaming, SSE handling, message rendering.
import { store } from './store.js';
import { copyToClipboard } from './ui.js';
import { loadSessions, updateSessionInSidebar } from './sessions.js';

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
    if (!el.dataset.highlighted) {
      hljs.highlightElement(el);
      el.dataset.highlighted = '1';
    }
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
  store.body.innerHTML = renderMarkdown(store.rawText);
  highlightCode(store.body);
}

// ---- Templates ---------------------------------------------------------
function cloneTemplate(id) {
  return document.getElementById(id).content.firstElementChild.cloneNode(true);
}

function makeTurn(role, ts) {
  const node = cloneTemplate('tmpl-turn');
  node.classList.add(role);
  node.querySelector('.role').textContent =
    role === 'user' ? 'You' : store.agent ?? 'Assistant';
  node.querySelector('.timestamp').textContent = formatTimestamp(ts);
  return node;
}

function formatTimestamp(ts) {
  // Accept seconds (float from backend) or ms.
  if (ts == null) ts = Date.now();
  const ms = ts > 1e12 ? ts : ts * 1000;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) {
    // Fallback to current time
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  }
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function isoTime() {
  return formatTimestamp(Date.now());
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

// ---- Render helpers ----------------------------------------------------
export function appendUserTurn(text) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return;
  const node = makeTurn('user');
  const body = document.createElement('div');
  body.className = 'body';
  body.textContent = text;
  node.querySelector('.bubble').appendChild(body);
  transcriptEl.appendChild(node);
  scrollDown();
}

function startAssistantTurn(ts) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return {};
  const node = makeTurn('assistant', ts);
  node.classList.add('streaming');
  transcriptEl.appendChild(node);
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
    store.bubble.appendChild(store.body);
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
    store.bubble.insertBefore(details, store.bubble.firstChild);
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
  store.bubble.appendChild(node);
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

function appendApproval(call) {
  if (!store.bubble) return;
  const node = cloneTemplate('tmpl-approval');
  node.querySelector('.approval-name').textContent = call.name;
  node.querySelector('.approval-args').textContent = formatArgs(call.arguments);
  const resolve = async (decision) => {
    node.classList.add('resolved');
    try {
      await fetch('/api/chat/approve', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ session_id: store.sessionId, call_id: call.id, decision }),
      });
    } catch (err) { console.error(err); }
  };
  node.querySelector('.approve').addEventListener('click', () => resolve('approve'));
  node.querySelector('.decline').addEventListener('click', () => resolve('deny'));
  store.bubble.appendChild(node);
  store.body = null;
  store.rawText = '';
  scrollDown();
}

function appendHandoff(from, to) {
  if (!store.bubble) return;
  const node = cloneTemplate('tmpl-handoff');
  node.querySelector('.handoff-text').textContent = `${from}  →  ${to}`;
  store.bubble.appendChild(node);
}

function appendUsage(usage) {
  if (!store.bubble || !usage) return;
  const node = cloneTemplate('tmpl-usage');
  const parts = [];
  if (usage.input_tokens) parts.push(`in ${usage.input_tokens}`);
  if (usage.output_tokens) parts.push(`out ${usage.output_tokens}`);
  node.querySelector('.usage-tokens').textContent = parts.join('  ·  ');
  store.bubble.appendChild(node);
}

function appendContextCompacted(data) {
  if (!store.bubble) return;
  const node = cloneTemplate('tmpl-context-compacted');
  const msg = data.summary ? 'Context compacted with summary.' : 'Context compacted.';
  node.querySelector('.context-text').textContent = msg;
  store.bubble.appendChild(node);
}

function appendRetry() {
  if (!store.bubble) return;
  const node = cloneTemplate('tmpl-retry');
  node.querySelector('.retry-btn').addEventListener('click', () => store.emit('retry'));
  store.bubble.appendChild(node);
}

function addCopyButton(bubble) {
  if (!bubble) return;
  // Remove existing copy button if any
  bubble.querySelector('.btn-copy')?.remove();
  const bodyEl = bubble.querySelector('.body');
  if (!bodyEl || !bodyEl.textContent?.trim()) return;

  const btn = cloneTemplate('tmpl-copy-btn');
  btn.addEventListener('click', async () => {
    const text = bodyEl.textContent || '';
    const ok = await copyToClipboard(text);
    if (ok) {
      btn.textContent = 'Copied';
      btn.classList.add('copied');
      setTimeout(() => {
        btn.textContent = 'Copy';
        btn.classList.remove('copied');
      }, 1500);
    }
  });
  // Place after the body
  bodyEl.insertAdjacentElement('afterend', btn);
}

// ---- History rendering --------------------------------------------------
export function renderHistory(entries) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return;
  transcriptEl.innerHTML = '';
  store.bubble = null;
  store.body = null;
  store.rawText = '';
  store.toolNodes.clear();

  const pendingResults = new Map();
  for (const it of entries) {
    if (it.type === 'tool' && it.role === 'tool' && it.tool_call_id)
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
      bubble.appendChild(body);
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
        summary.innerHTML = '<span class="reasoning-icon">💭</span><span class="reasoning-label">Reasoning</span>';
        details.appendChild(summary);
        const rc = document.createElement('div');
        rc.className = 'reasoning-content';
        rc.textContent = it.reasoning;
        details.appendChild(rc);
        currentBubble.appendChild(details);
      }
      if (text) {
        const body = document.createElement('div');
        body.className = 'body';
        body.innerHTML = renderMarkdown(text);
        currentBubble.appendChild(body);
        highlightCode(body);
      }
      if (it.tool_calls) {
        for (const call of it.tool_calls) {
          const node = buildToolNode(call);
          const result = pendingResults.get(call.id);
          if (result !== undefined && result !== '') {
            node.querySelector('.tool-result').textContent = result;
          } else {
            // No result stored — hide the empty <pre>
            const pre = node.querySelector('.tool-result');
            if (pre) pre.style.display = 'none';
          }
          currentBubble.appendChild(node);
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
  scrollDown();
}

// ---- Scroll ------------------------------------------------------------
let _userScrolled = false;
function _isAtBottom() {
  const el = document.getElementById('transcript');
  return el.scrollHeight - el.scrollTop - el.clientHeight < 50;
}
function scrollDown() {
  if (_userScrolled) return;
  requestAnimationFrame(() => {
    document.getElementById('transcript')?.scrollTo({ top: document.getElementById('transcript').scrollHeight, behavior: 'smooth' });
  });
}
document.getElementById('transcript')?.addEventListener('scroll', () => {
  _userScrolled = !_isAtBottom();
}, { passive: true });

// ---- SSE ---------------------------------------------------------------
function parseSSE(chunk) {
  const lines = chunk.split('\n');
  let event = 'message', data = '';
  for (const line of lines) {
    if (line.startsWith(':')) continue;
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) data += (data ? '\n' : '') + line.slice(5).replace(/^ /, '');
  }
  if (!data) return null;
  try { return { event, data: JSON.parse(data) }; }
  catch { return { event, data }; }
}

const turnProgressEl = document.getElementById('turn-progress');

async function handleEvent({ event, data }) {
  switch (event) {
    case 'session':
      store.sessionId = data.session_id;
      store.syncURL(data.session_id);
      break;

    case 'text_delta':
      finalizeReasoning();
      ensureBody();
      store.rawText += data.delta;
      scheduleRender();
      scrollDown();
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

    case 'tool_call':
      finalizeReasoning();
      appendTool(data);
      break;

    case 'tool_result':
      updateToolResult(data.id, data.result, data.is_error);
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

    case 'title':
      if (data.title) {
        document.getElementById('chat-title').textContent = data.title;
        updateSessionInSidebar(store.sessionId, data.title);
      }
      break;

    case 'error':
      ensureBody();
      store.rawText += `\n\n> ⚠️ **Error:** ${data.message}`;
      flushRender();
      appendRetry();
      break;

    case 'done':
      if (turnProgressEl) turnProgressEl.classList.add('hidden');
      appendUsage(data.usage);
      break;
  }
}

// ---- Streaming ---------------------------------------------------------
const sendBtn = document.getElementById('send');
const stopBtn = document.getElementById('stop');
const composer = document.getElementById('composer');
const promptEl = document.getElementById('prompt');
let _streamAbortController = null;

export async function runStream(message) {
  store.streaming = true;
  store.lastMessage = message;
  sendBtn.style.display = 'none';
  if (stopBtn) stopBtn.style.display = '';
  const { node: turn, bubble } = startAssistantTurn();

  _userScrolled = false;
  _streamAbortController = new AbortController();

  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
      body: JSON.stringify({ message, agent: store.agent, session_id: store.sessionId }),
      signal: _streamAbortController.signal,
    });

    if (!res.ok || !res.body) {
      ensureBody().innerHTML = `<span class="error-text">Error: ${res.status} ${res.statusText}</span>`;
      appendRetry();
      return;
    }

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let raw = '';
    while (true) {
      const { value, done } = await reader.read();
      if (value?.length) {
        raw += dec.decode(value, { stream: !done });
        raw = raw.replace(/\r\n/g, '\n');
        let idx;
        while ((idx = raw.indexOf('\n\n')) >= 0) {
          const chunk = raw.slice(0, idx);
          raw = raw.slice(idx + 2);
          const ev = parseSSE(chunk);
          if (ev) await handleEvent(ev);
        }
      }
      if (done) break;
    }
  } catch (err) {
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
    if (store.body && store.rawText) flushRender();
    if (turn) {
      turn.classList.remove('streaming');
      // Add copy button to final bubble
      addCopyButton(bubble);
    }
    store.streaming = false;
    _streamAbortController = null;
    sendBtn.style.display = '';
    if (stopBtn) stopBtn.style.display = 'none';
    if (promptEl) promptEl.focus();
    if (turnProgressEl) turnProgressEl.classList.add('hidden');
    // Title is generated in a background task after the stream closes.
    // Poll with back-off: 0.6, 1.2, 2.0, 3.5, 6, 12 s, then every
    // 10 s until the user switches sessions.  This covers both fast
    // title generation (1-2 s) and slow/queued LLM calls (30+ s).
    let _pollAttempt = 0;
    const _pollSid = store.sessionId;
    const _doPoll = () => {
      if (!_pollSid || store.sessionId !== _pollSid) return;
      _pollAttempt++;
      loadSessions();
      let next;
      if (_pollAttempt <= 6) {
        next = [600, 600, 800, 1500, 2500, 6000][_pollAttempt - 1];
      } else {
        next = 10_000; // keep checking every 10 s forever
      }
      setTimeout(_doPoll, next);
    };
    loadSessions();
    setTimeout(_doPoll, 600);
  }
}

export async function cancelStream() {
  if (_streamAbortController) _streamAbortController.abort();
  if (store.sessionId) {
    try {
      await fetch(`/api/chat/cancel?session_id=${encodeURIComponent(store.sessionId)}`, { method: 'POST' });
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
    if (store.streaming) return;
    const message = promptEl.value.trim();
    if (!message) return;
    promptEl.value = '';
    autoresize();
    _userScrolled = false;
    document.getElementById('empty-state')?.remove();
    appendUserTurn(message);
    await runStream(message);
  });

  stopBtn?.addEventListener('click', () => cancelStream());
}
