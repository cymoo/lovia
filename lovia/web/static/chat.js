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
const toggleRight = $("#toggle-right");
const sidebarRight = $("#sidebar-right");
const panelFiles = $("#panel-files");
const panelAudit = $("#panel-audit");
const fileModal = $("#file-modal");

const state = {
  sessionId: null,
  agent: null,
  agents: [],
  sessions: [],
  streaming: false,
  bubble: null,
  body: null,
  toolNodes: new Map(),
  panelTimer: null,
};

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
    renderHistory(data.items || []);
  } catch (err) {
    transcript.innerHTML = `<div class="empty-state"><h2>Couldn't load chat</h2><p>${err.message ?? err}</p></div>`;
  }
  renderSessions();
  refreshPanels();
}

function clearChat() {
  state.sessionId = null;
  state.bubble = null;
  state.body = null;
  state.toolNodes.clear();
  chatTitleEl.textContent = "New chat";
  transcript.innerHTML = `
    <div class="empty-state" id="empty-state">
      <h2>How can I help you today?</h2>
      <p>Ask anything — I'll think it through and respond.</p>
    </div>`;
  refreshPanels();
}

newChatBtn.addEventListener("click", () => {
  clearChat();
  promptEl.focus();
});

// ------------------------------------------------ history rendering ----

function renderHistory(items) {
  transcript.innerHTML = "";
  state.bubble = null;
  state.body = null;
  state.toolNodes.clear();

  const pendingResults = new Map(); // call_id → result text

  // First pass: collect tool results.
  for (const it of items) {
    if (it.type === "tool" && it.role === "tool" && it.tool_call_id) {
      pendingResults.set(it.tool_call_id, contentText(it.content));
    }
  }

  let currentBubble = null;
  for (const it of items) {
    if (it.role === "user") {
      currentBubble = null;
      appendUserTurn(contentText(it.content));
    } else if (it.role === "assistant") {
      // Each assistant message creates (or continues) the bubble.
      if (!currentBubble) currentBubble = startAssistantTurn().bubble;
      const text = contentText(it.content);
      if (text) {
        const body = document.createElement("div");
        body.className = "body";
        body.textContent = text;
        currentBubble.appendChild(body);
      }
      if (it.tool_calls) {
        for (const call of it.tool_calls) {
          const node = buildToolNode(call);
          const result = pendingResults.get(call.id);
          if (result !== undefined) {
            node.querySelector(".tool-result").textContent = result;
          }
          currentBubble.appendChild(node);
        }
      }
    }
  }
  // Drop the lingering streaming marker.
  $$(".turn.streaming").forEach((n) => n.classList.remove("streaming"));
  state.bubble = null;
  state.body = null;
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
  state.toolNodes.clear();
  scrollDown();
  return { node, bubble: state.bubble };
}

function ensureBody() {
  if (!state.body && state.bubble) {
    const body = document.createElement("div");
    body.className = "body";
    state.bubble.appendChild(body);
    state.body = body;
  }
  return state.body;
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

function scrollDown() {
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
      if (done) break;
      raw += dec.decode(value, { stream: true });
      raw = raw.replace(/\r\n/g, "\n");
      let idx;
      while ((idx = raw.indexOf("\n\n")) >= 0) {
        const chunk = raw.slice(0, idx);
        raw = raw.slice(idx + 2);
        const ev = parseSSE(chunk);
        if (ev) handleEvent(ev);
      }
    }
  } catch (err) {
    ensureBody().textContent += `\n[error] ${err.message ?? err}`;
  } finally {
    turn.classList.remove("streaming");
    state.streaming = false;
    sendBtn.disabled = false;
    promptEl.focus();
    refreshPanels();
    loadSessions();
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

function handleEvent({ event, data }) {
  switch (event) {
    case "session":
      state.sessionId = data.session_id;
      break;
    case "text_delta":
      ensureBody().textContent += data.delta;
      scrollDown();
      break;
    case "message_completed":
      state.body = null;
      break;
    case "tool_call":
      appendTool(data);
      // A tool call usually means new files / audit entries.
      schedulePanelRefresh();
      break;
    case "tool_result":
      updateToolResult(data.id, data.result, data.is_error);
      schedulePanelRefresh();
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
      ensureBody().textContent += `\n[error] ${data.message}`;
      break;
    case "done":
      break;
  }
}

// ---------------------------------------------- right panel: files/audit

function schedulePanelRefresh() {
  if (state.panelTimer) return;
  state.panelTimer = setTimeout(() => {
    state.panelTimer = null;
    refreshPanels();
  }, 400);
}

async function refreshPanels() {
  if (!state.sessionId) {
    panelFiles.innerHTML = '<div class="panel-empty">No workspace yet.</div>';
    panelAudit.innerHTML = '<div class="panel-empty">No audited commands.</div>';
    return;
  }
  await Promise.all([refreshFiles(), refreshAudit()]);
}

async function refreshFiles() {
  try {
    const res = await fetch(`/api/sessions/${state.sessionId}/files`);
    if (!res.ok) {
      panelFiles.innerHTML = '<div class="panel-empty">No workspace.</div>';
      return;
    }
    const entries = await res.json();
    renderFiles(entries);
  } catch (err) {
    panelFiles.innerHTML = `<div class="panel-empty">Files unavailable</div>`;
  }
}

function renderFiles(entries) {
  if (!entries.length) {
    panelFiles.innerHTML = '<div class="panel-empty">Empty workspace.</div>';
    return;
  }
  panelFiles.innerHTML = "";
  for (const e of entries) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "file-row";
    row.disabled = e.is_dir;
    row.innerHTML = `
      <span class="file-icon">${e.is_dir ? "📁" : "📄"}</span>
      <span class="file-name"></span>
      <span class="file-size">${e.is_dir ? "" : formatBytes(e.size)}</span>`;
    row.querySelector(".file-name").textContent = e.name;
    row.addEventListener("click", () => openFile(e.name));
    panelFiles.appendChild(row);
  }
}

async function openFile(name) {
  try {
    const res = await fetch(
      `/api/sessions/${state.sessionId}/files/${encodeURIComponent(name)}`
    );
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    $("#file-modal-path").textContent = data.path;
    $("#file-modal-body").textContent = data.content;
    fileModal.classList.remove("hidden");
  } catch (err) {
    console.error(err);
  }
}

$("#file-modal-close").addEventListener("click", () => {
  fileModal.classList.add("hidden");
});
fileModal.addEventListener("click", (e) => {
  if (e.target === fileModal) fileModal.classList.add("hidden");
});

async function refreshAudit() {
  try {
    const res = await fetch(`/api/sessions/${state.sessionId}/audit`);
    if (!res.ok) {
      panelAudit.innerHTML = '<div class="panel-empty">No audit log.</div>';
      return;
    }
    const entries = await res.json();
    renderAudit(entries);
  } catch {
    panelAudit.innerHTML = '<div class="panel-empty">Audit unavailable.</div>';
  }
}

function renderAudit(entries) {
  if (!entries.length) {
    panelAudit.innerHTML = '<div class="panel-empty">No audited commands.</div>';
    return;
  }
  panelAudit.innerHTML = "";
  for (const e of [...entries].reverse()) {
    const row = document.createElement("div");
    row.className = `audit-row decision-${e.verdict}`;
    row.innerHTML = `
      <div class="audit-head">
        <span class="audit-badge"></span>
        <span class="audit-rule"></span>
        <span class="audit-time"></span>
      </div>
      <pre class="audit-cmd"></pre>
      <div class="audit-reason"></div>`;
    row.querySelector(".audit-badge").textContent = e.verdict.toUpperCase();
    row.querySelector(".audit-rule").textContent = e.tool_name || "—";
    row.querySelector(".audit-time").textContent = formatTime(e.timestamp);
    row.querySelector(".audit-cmd").textContent = e.command;
    const reason = row.querySelector(".audit-reason");
    if (e.reason) reason.textContent = e.reason;
    else reason.remove();
    panelAudit.appendChild(row);
  }
}

// ---------------------------------------------------- panel tab toggle -

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    const name = tab.dataset.tab;
    panelFiles.classList.toggle("hidden", name !== "files");
    panelAudit.classList.toggle("hidden", name !== "audit");
  });
});

toggleRight.addEventListener("click", () => {
  sidebarRight.classList.toggle("collapsed");
});

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

function formatBytes(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// ------------------------------------------------------------- bootstrap

(async function () {
  await Promise.all([loadAgents(), loadSessions()]);
  promptEl.focus();
})();
