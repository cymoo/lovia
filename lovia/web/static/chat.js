// lovia chat — vanilla JS, no build step.
// Talks to /api/agents, /api/chat/stream, /api/chat/approve, /api/sessions/*.

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

const transcript = $("#transcript");
const composer = $("#composer");
const promptEl = $("#prompt");
const sendBtn = $("#send");
const agentLabel = $("#agent-label");
const newChatBtn = $("#new-chat");
const sessionsList = $("#sessions-list");
const chatTitleEl = $("#chat-title");

const state = {
  sessionId: null,
  agent: null,
  agents: [],
  sessions: [],
  streaming: false,
  bubble: null,
  body: null,
  rawText: "",       // accumulated markdown text for the current body
  toolNodes: new Map(),
  reasoningText: "", // accumulated chain-of-thought text
  reasoningNode: null,
  reasoningStart: 0, // timestamp when reasoning actually started
  reasoningEnd: 0,   // timestamp of last reasoning delta
};

// --------------------------------------------------------------- markdown -

marked.setOptions({ gfm: true, breaks: false });

function renderMarkdown(text) {
  if (!text.trim()) return "";
  const html = marked.parse(text);
  return typeof DOMPurify !== "undefined" ? DOMPurify.sanitize(html) : html;
}

// Streaming render: debounce so we don't re-render on every single token.
let _renderTimer = null;
function scheduleRender() {
  clearTimeout(_renderTimer);
  _renderTimer = setTimeout(flushRender, 60);
}
function flushRender() {
  if (!state.body || !state.rawText) return;
  state.body.innerHTML = renderMarkdown(state.rawText);
}

// --------------------------------------------------------------- agents -

async function loadAgents() {
  try {
    const res = await fetch("/api/agents");
    state.agents = await res.json();
    state.agent = state.agents[0]?.name ?? null;
    agentLabel.textContent = state.agent ?? "no agent";
  } catch {
    agentLabel.textContent = "offline";
  }
}

// ------------------------------------------------------------- sessions -

async function loadSessions() {
  try {
    const res = await fetch("/api/sessions");
    state.sessions = await res.json();
    renderSessions();
  } catch (err) {
    console.error("loadSessions:", err);
  }
}

function renderSessions() {
  sessionsList.innerHTML = "";
  if (!state.sessions.length) {
    const empty = document.createElement("div");
    empty.className = "sessions-empty";
    empty.textContent = "No chats yet.";
    sessionsList.appendChild(empty);
    return;
  }
  for (const s of state.sessions) {
    const item = document.createElement("div");
    item.className = "session-item";
    if (s.id === state.sessionId) item.classList.add("active");
    item.dataset.id = s.id;

    const main = document.createElement("button");
    main.type = "button";
    main.className = "session-main";
    main.title = s.title || s.id;
    main.innerHTML = `
      <div class="session-title"></div>
      <div class="session-meta"></div>`;
    main.querySelector(".session-title").textContent = s.title || "New chat";
    main.querySelector(".session-meta").textContent = formatTime(s.updated_at);
    main.addEventListener("click", () => switchSession(s.id));

    const menu = document.createElement("div");
    menu.className = "session-menu";
    const rename = document.createElement("button");
    rename.type = "button";
    rename.title = "Rename";
    rename.innerHTML = "✎";
    rename.addEventListener("click", (e) => {
      e.stopPropagation();
      renameSession(s);
    });
    const del = document.createElement("button");
    del.type = "button";
    del.title = "Delete";
    del.innerHTML = "✕";
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteSession(s.id);
    });
    menu.append(rename, del);

    item.append(main, menu);
    sessionsList.appendChild(item);
  }

  // Keep the header in sync: if the active session now has a title, show it.
  if (state.sessionId) {
    const active = state.sessions.find((s) => s.id === state.sessionId);
    if (active?.title) chatTitleEl.textContent = active.title;
  }
}

async function renameSession(s) {
  const next = window.prompt("Rename chat:", s.title || "");
  if (next === null) return;
  const title = next.trim();
  if (!title) return;
  try {
    await fetch(`/api/sessions/${s.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title }),
    });
    if (state.sessionId === s.id) chatTitleEl.textContent = title;
    await loadSessions();
  } catch (err) {
    console.error(err);
  }
}

async function deleteSession(id) {
  if (!window.confirm("Delete this chat?")) return;
  try {
    await fetch(`/api/sessions/${id}`, { method: "DELETE" });
  } catch (err) {
    console.error(err);
  }
  if (state.sessionId === id) clearChat();
  await loadSessions();
}

async function switchSession(id) {
  if (state.streaming || state.sessionId === id) return;
  state.sessionId = id;
  $("#empty-state")?.remove();
  transcript.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const res = await fetch(`/api/sessions/${id}`);
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    chatTitleEl.textContent = data.title || "New chat";
    renderHistory(data.entries || []);
  } catch (err) {
    transcript.innerHTML = `<div class="empty-state"><h2>Couldn't load chat</h2><p>${err.message ?? err}</p></div>`;
  }
  renderSessions();
}

function clearChat() {
  state.sessionId = null;
  state.bubble = null;
  state.body = null;
  state.rawText = "";
  state.toolNodes.clear();
  chatTitleEl.textContent = "New chat";
  transcript.innerHTML = `
    <div class="empty-state" id="empty-state">
      <h2>How can I help?</h2>
      <p>Ask a question, approve tools when needed, and keep the thread.</p>
    </div>`;
}

newChatBtn.addEventListener("click", () => {
  clearChat();
  promptEl.focus();
});

// ------------------------------------------------ history rendering ----

function renderHistory(entries) {
  transcript.innerHTML = "";
  state.bubble = null;
  state.body = null;
  state.rawText = "";
  state.toolNodes.clear();

  const pendingResults = new Map(); // call_id → result text

  // First pass: collect tool results.
  for (const it of entries) {
    if (it.type === "tool" && it.role === "tool" && it.tool_call_id) {
      pendingResults.set(it.tool_call_id, contentText(it.content));
    }
  }

  let currentBubble = null;
  for (const it of entries) {
    if (it.role === "user") {
      currentBubble = null;
      appendUserTurn(contentText(it.content));
    } else if (it.role === "assistant") {
      if (!currentBubble) currentBubble = startAssistantTurn().bubble;
      const text = contentText(it.content);
      // Render reasoning first (before the body text).
      if (it.reasoning) {
        const details = document.createElement("details");
        details.className = "reasoning done";
        const summary = document.createElement("summary");
        summary.innerHTML = '<span class="reasoning-icon">💭</span><span class="reasoning-label">Reasoning</span>';
        details.appendChild(summary);
        const reasoningContent = document.createElement("div");
        reasoningContent.className = "reasoning-content";
        reasoningContent.textContent = it.reasoning;
        details.appendChild(reasoningContent);
        currentBubble.appendChild(details);
      }
      if (text) {
        const body = document.createElement("div");
        body.className = "body";
        body.innerHTML = renderMarkdown(text);
        currentBubble.appendChild(body);
      }
      if (it.tool_calls) {
        for (const call of it.tool_calls) {
          const node = buildToolNode(call);
          const result = pendingResults.get(call.id);
          if (result !== undefined) node.querySelector(".tool-result").textContent = result;
          currentBubble.appendChild(node);
        }
      }
    }
  }
  // Drop the lingering streaming marker.
  $$(".turn.streaming").forEach((n) => n.classList.remove("streaming"));
  state.bubble = null;
  state.body = null;
  state.rawText = "";
  scrollDown();
}

function contentText(content) {
  if (content == null) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((p) => (typeof p === "string" ? p : p.text ?? ""))
      .join("");
  }
  return String(content);
}

// ------------------------------------------------- composer / streaming -

const autoresize = () => {
  promptEl.style.height = "auto";
  promptEl.style.height =
    Math.min(promptEl.scrollHeight, window.innerHeight * 0.3) + "px";
};
promptEl.addEventListener("input", autoresize);
promptEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.streaming) return;
  const message = promptEl.value.trim();
  if (!message) return;
  promptEl.value = "";
  autoresize();
  _userScrolled = false;
  $("#empty-state")?.remove();
  appendUserTurn(message);
  await runStream(message);
});

function makeTurn(role) {
  const node = $("#tmpl-turn").content.firstElementChild.cloneNode(true);
  node.classList.add(role);
  node.querySelector(".role").textContent =
    role === "user" ? "You" : state.agent ?? "Assistant";
  return node;
}

function appendUserTurn(text) {
  const node = makeTurn("user");
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text;
  node.querySelector(".bubble").appendChild(body);
  transcript.appendChild(node);
  scrollDown();
}

function startAssistantTurn() {
  const node = makeTurn("assistant");
  node.classList.add("streaming");
  transcript.appendChild(node);
  state.bubble = node.querySelector(".bubble");
  state.body = null;
  state.rawText = "";
  state.toolNodes.clear();
  state.reasoningText = "";
  state.reasoningNode = null;
  state.reasoningStart = 0;
  state.reasoningEnd = 0;
  scrollDown();
  return { node, bubble: state.bubble };
}

function ensureBody() {
  if (!state.body && state.bubble) {
    const body = document.createElement("div");
    body.className = "body";
    state.bubble.appendChild(body);
    state.body = body;
    state.rawText = "";
  }
  return state.body;
}

function ensureReasoning() {
  if (!state.reasoningNode && state.bubble) {
    const details = document.createElement("details");
    details.className = "reasoning";
    details.open = true; // expanded during streaming
    const summary = document.createElement("summary");
    summary.innerHTML = '<span class="reasoning-icon">💭</span><span class="reasoning-label">Thinking…</span>';
    details.appendChild(summary);
    const content = document.createElement("div");
    content.className = "reasoning-content";
    details.appendChild(content);
    // Insert at the top of the bubble, before body and tools.
    state.bubble.insertBefore(details, state.bubble.firstChild);
    state.reasoningNode = details;
  }
  return state.reasoningNode;
}

function finalizeReasoning() {
  /* Collapse the reasoning panel and stamp it with a duration label.
     Called as soon as reasoning stops — i.e. when text or tool calls
     begin — so the user sees the timing immediately, not after the
     entire turn finishes. */
  if (!state.reasoningNode || !state.reasoningText) return;
  state.reasoningNode.open = false;
  state.reasoningNode.classList.add("done");
  const end = state.reasoningEnd || Date.now();
  const start = state.reasoningStart || end;
  const elapsed = ((end - start) / 1000).toFixed(1);
  const label = state.reasoningNode.querySelector(".reasoning-label");
  if (label) label.textContent = `Thought for ${elapsed}s`;
}

function buildToolNode(call) {
  const node = $("#tmpl-tool").content.firstElementChild.cloneNode(true);
  node.querySelector(".tool-name").textContent = call.name;
  node.querySelector(".tool-args").textContent = formatArgs(call.arguments);
  return node;
}

function appendTool(call) {
  if (!state.bubble) return;
  const node = buildToolNode(call);
  state.bubble.appendChild(node);
  state.toolNodes.set(call.id, node);
  state.body = null;
  state.rawText = "";
  scrollDown();
}

function updateToolResult(id, result, isError) {
  const node = state.toolNodes.get(id);
  if (!node) return;
  node.querySelector(".tool-result").textContent = String(result);
  if (isError) node.classList.add("error");
}

function appendApproval(call) {
  if (!state.bubble) return;
  const node = $("#tmpl-approval").content.firstElementChild.cloneNode(true);
  node.querySelector(".approval-name").textContent = call.name;
  node.querySelector(".approval-args").textContent = formatArgs(call.arguments);
  const resolve = async (decision) => {
    node.classList.add("resolved");
    try {
      await fetch("/api/chat/approve", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          session_id: state.sessionId,
          call_id: call.id,
          decision,
        }),
      });
    } catch (err) {
      console.error(err);
    }
  };
  node.querySelector(".approve").addEventListener("click", () => resolve("approve"));
  node.querySelector(".decline").addEventListener("click", () => resolve("deny"));
  state.bubble.appendChild(node);
  state.body = null;
  state.rawText = "";
  scrollDown();
}

function formatArgs(args) {
  if (!args) return "()";
  try {
    const obj = JSON.parse(args);
    const entries = Object.entries(obj);
    if (entries.length === 0) return "()";
    return (
      "(" +
      entries.map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join(", ") +
      ")"
    );
  } catch {
    return `(${args})`;
  }
}

// Auto-scroll: disabled when the user has scrolled up, re-enabled when they
// return to the bottom.
let _userScrolled = false;

function _isAtBottom() {
  return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 50;
}

transcript.addEventListener("scroll", () => {
  _userScrolled = !_isAtBottom();
}, { passive: true });

function scrollDown() {
  if (_userScrolled) return;
  requestAnimationFrame(() => {
    transcript.scrollTo({ top: transcript.scrollHeight, behavior: "smooth" });
  });
}

async function runStream(message) {
  state.streaming = true;
  sendBtn.disabled = true;
  const { node: turn } = startAssistantTurn();

  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "text/event-stream",
      },
      body: JSON.stringify({
        message,
        agent: state.agent,
        session_id: state.sessionId,
      }),
    });
    if (!res.ok || !res.body) {
      ensureBody().textContent = `Error: ${res.status} ${res.statusText}`;
      return;
    }

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let raw = "";
    while (true) {
      const { value, done } = await reader.read();
      // Process bytes before checking done — some browsers deliver the final
      // chunk together with done: true (spec-violating but observed in practice).
      if (value?.length) {
        raw += dec.decode(value, { stream: !done });
        raw = raw.replace(/\r\n/g, "\n");
        let idx;
        while ((idx = raw.indexOf("\n\n")) >= 0) {
          const chunk = raw.slice(0, idx);
          raw = raw.slice(idx + 2);
          const ev = parseSSE(chunk);
          if (ev) await handleEvent(ev);
        }
      }
      if (done) break;
    }
  } catch (err) {
    ensureBody().textContent += `\n[error] ${err.message ?? err}`;
  } finally {
    // Flush any pending debounced render before removing the streaming class.
    clearTimeout(_renderTimer);
    if (state.body && state.rawText) flushRender();
    turn.classList.remove("streaming");
    state.streaming = false;
    sendBtn.disabled = false;
    promptEl.focus();
    loadSessions();
    // Background title generation may not be done yet; poll once more shortly.
    const sid = state.sessionId;
    setTimeout(() => { if (state.sessionId === sid) loadSessions(); }, 3000);
  }
}

function parseSSE(chunk) {
  const lines = chunk.split("\n");
  let event = "message";
  let data = "";
  for (const line of lines) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) {
      data += (data ? "\n" : "") + line.slice(5).replace(/^ /, "");
    }
  }
  if (!data) return null;
  try {
    return { event, data: JSON.parse(data) };
  } catch {
    return { event, data };
  }
}

async function handleEvent({ event, data }) {
  switch (event) {
    case "session":
      state.sessionId = data.session_id;
      break;
    case "text_delta":
      finalizeReasoning();
      ensureBody();
      state.rawText += data.delta;
      scheduleRender();
      scrollDown();
      break;
    case "reasoning_delta":
      ensureReasoning();
      if (!state.reasoningStart) state.reasoningStart = Date.now();
      state.reasoningEnd = Date.now();
      state.reasoningText += data.delta;
      state.reasoningNode.querySelector(".reasoning-content").textContent = state.reasoningText;
      scrollDown();
      break;
    case "message_completed":
      // Cancel debounced render and do a final synchronous render.
      clearTimeout(_renderTimer);
      if (state.body && state.rawText) flushRender();
      state.body = null;
      state.rawText = "";
      // If reasoning arrived only in the completed message (no deltas),
      // render and finalize it now.
      if (!state.reasoningNode && data.message?.reasoning && state.bubble) {
        ensureReasoning();
        state.reasoningText = data.message.reasoning;
        state.reasoningNode.querySelector(".reasoning-content").textContent = state.reasoningText;
        state.reasoningEnd = Date.now();
      }
      // Safety net: if reasoning still hasnʼt been finalized (e.g. no
      // text_delta or tool_call followed it), finalize now.
      finalizeReasoning();
      state.reasoningNode = null;
      state.reasoningText = "";
      state.reasoningStart = 0;
      state.reasoningEnd = 0;
      break;
    case "tool_call":
      finalizeReasoning();
      appendTool(data);
      break;
    case "tool_result":
      updateToolResult(data.id, data.result, data.is_error);
      break;
    case "approval_required":
      appendApproval(data);
      break;
    case "title":
      if (data.title) {
        chatTitleEl.textContent = data.title;
        loadSessions();
      }
      break;
    case "error":
      ensureBody();
      state.rawText += `\n[error] ${data.message}`;
      flushRender();
      break;
    case "done":
      break;
  }
}

// ----------------------------------------------------------- utilities -

function formatTime(ts) {
  if (!ts) return "";
  // Accept either seconds (epoch float) or ms.
  const ms = ts > 1e12 ? ts : ts * 1000;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return String(ts);
  const now = new Date();
  const same = d.toDateString() === now.toDateString();
  return same
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString();
}

// ------------------------------------------------------------- bootstrap

(async function () {
  await Promise.all([loadAgents(), loadSessions()]);
  promptEl.focus();
})();
