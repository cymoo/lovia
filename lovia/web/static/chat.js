// lovia chat — vanilla JS module, no build step.
// Talks to /api/agents, /api/chat/stream, /api/chat/approve.

const $ = (sel, root = document) => root.querySelector(sel);
const transcript = $("#transcript");
const composer = $("#composer");
const promptEl = $("#prompt");
const sendBtn = $("#send");
const agentLabel = $("#agent-label");
const resetBtn = $("#reset");

const state = {
  sessionId: null,
  agent: null,
  agents: [],
  streaming: false,
  // streaming targets — explicitly tracked so DOM lookups never go stale
  bubble: null,
  body: null,
  toolNodes: new Map(), // id → details element
};

// ---------------------------------------------------------------- init -

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

resetBtn.addEventListener("click", async () => {
  if (state.sessionId) {
    try { await fetch(`/api/sessions/${state.sessionId}`, { method: "DELETE" }); } catch {}
  }
  state.sessionId = null;
  state.bubble = null;
  state.body = null;
  state.toolNodes.clear();
  transcript.innerHTML = `
    <div class="empty-state" id="empty-state">
      <h1>How can I help you today?</h1>
      <p>Ask anything — I'll think it through and respond.</p>
    </div>`;
  promptEl.focus();
});

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

// --------------------------------------------------- turn-level helpers -

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
  state.body = null; // created lazily on first text_delta
  state.toolNodes.clear();
  scrollDown();
  return node;
}

function ensureBody() {
  // Called by text_delta — if a MessageCompleted has cleared `body`,
  // create a fresh body div inside the same bubble for the next message.
  if (!state.body && state.bubble) {
    const body = document.createElement("div");
    body.className = "body";
    state.bubble.appendChild(body);
    state.body = body;
  }
  return state.body;
}

function appendTool(call) {
  if (!state.bubble) return;
  const node = $("#tmpl-tool").content.firstElementChild.cloneNode(true);
  node.querySelector(".tool-name").textContent = call.name;
  node.querySelector(".tool-args").textContent = formatArgs(call.arguments);
  state.bubble.appendChild(node);
  state.toolNodes.set(call.id, node);
  // Tool nodes act as delimiters too — start a new body for any text after.
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
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  });
}

// ----------------------------------------------------------- streaming -

async function runStream(message) {
  state.streaming = true;
  sendBtn.disabled = true;
  const turn = startAssistantTurn();

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
      // Spec: events separated by blank line. Servers may use \r\n or \n.
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
  }
}

function parseSSE(chunk) {
  const lines = chunk.split("\n");
  let event = "message";
  let data = "";
  for (const line of lines) {
    if (line.startsWith(":")) continue; // comment / keepalive
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
      // Next text_delta starts a fresh body inside the same bubble.
      state.body = null;
      break;
    case "tool_call":
      appendTool(data);
      break;
    case "tool_result":
      updateToolResult(data.id, data.result, data.is_error);
      break;
    case "approval_required":
      appendApproval(data);
      break;
    case "error":
      ensureBody().textContent += `\n[error] ${data.message}`;
      break;
    case "done":
      break;
  }
}

loadAgents().then(() => promptEl.focus());
