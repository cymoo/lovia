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
};

// ---------------------------------------------------------------- init -

async function loadAgents() {
  try {
    const res = await fetch("/api/agents");
    state.agents = await res.json();
    state.agent = state.agents[0]?.name ?? null;
    agentLabel.textContent = state.agent ? `agent · ${state.agent}` : "no agent";
  } catch {
    agentLabel.textContent = "offline";
  }
}

resetBtn.addEventListener("click", async () => {
  if (state.sessionId) {
    try { await fetch(`/api/sessions/${state.sessionId}`, { method: "DELETE" }); } catch {}
  }
  state.sessionId = null;
  transcript.innerHTML = "";
  promptEl.focus();
});

promptEl.addEventListener("input", () => {
  promptEl.style.height = "auto";
  promptEl.style.height = Math.min(promptEl.scrollHeight, window.innerHeight * 0.3) + "px";
});
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
  promptEl.style.height = "auto";
  appendTurn("user", message);
  await runStream(message);
});

// --------------------------------------------------- turn-level helpers -

function appendTurn(role, text = "") {
  const tmpl = $("#tmpl-turn");
  const node = tmpl.content.firstElementChild.cloneNode(true);
  node.classList.add(role);
  node.querySelector(".role").textContent = role;
  node.querySelector(".body").textContent = text;
  transcript.appendChild(node);
  node.scrollIntoView({ behavior: "smooth", block: "end" });
  return node;
}

function appendTool(name, args) {
  const tmpl = $("#tmpl-tool");
  const node = tmpl.content.firstElementChild.cloneNode(true);
  node.querySelector(".tool-name").textContent = name;
  node.querySelector(".tool-args").textContent = ` (${truncate(args, 60)})`;
  transcript.appendChild(node);
  return node;
}

function appendApproval(call) {
  const tmpl = $("#tmpl-approval");
  const node = tmpl.content.firstElementChild.cloneNode(true);
  node.querySelector(".approval-name").textContent = call.name;
  node.querySelector(".approval-args").textContent = call.arguments;
  const resolve = async (decision) => {
    node.querySelectorAll("button").forEach((b) => (b.disabled = true));
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
    node.querySelector(".label").textContent = `decision · ${decision}`;
  };
  node.querySelector(".approve").addEventListener("click", () => resolve("approve"));
  node.querySelector(".decline").addEventListener("click", () => resolve("deny"));
  transcript.appendChild(node);
  return node;
}

function truncate(s, n) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// ----------------------------------------------------------- streaming -

async function runStream(message) {
  state.streaming = true;
  sendBtn.disabled = true;

  const assistant = appendTurn("assistant", "");
  assistant.classList.add("streaming");
  const body = assistant.querySelector(".body");

  let buffer = "";

  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: JSON.stringify({
        message,
        agent: state.agent,
        session_id: state.sessionId,
      }),
    });
    if (!res.ok || !res.body) {
      body.textContent = `error: ${res.status}`;
      return;
    }

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let raw = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      raw += dec.decode(value, { stream: true });
      // SSE: events separated by blank line
      let idx;
      while ((idx = raw.indexOf("\n\n")) >= 0) {
        const chunk = raw.slice(0, idx);
        raw = raw.slice(idx + 2);
        const ev = parseSSE(chunk);
        if (!ev) continue;
        handleEvent(ev, { body, buffer });
        // buffer is updated inside handleEvent through closure-by-ref; rebind
        buffer = body.textContent;
      }
    }
  } catch (err) {
    body.textContent += `\n[error] ${err.message ?? err}`;
  } finally {
    assistant.classList.remove("streaming");
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
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trimStart();
  }
  if (!data) return null;
  try {
    return { event, data: JSON.parse(data) };
  } catch {
    return { event, data };
  }
}

function handleEvent({ event, data }) {
  const lastAssistant = transcript.querySelector(".turn.assistant.streaming .body");

  switch (event) {
    case "session":
      state.sessionId = data.session_id;
      break;
    case "text_delta":
      if (lastAssistant) lastAssistant.textContent += data.delta;
      break;
    case "tool_call":
      appendTool(data.name, data.arguments);
      break;
    case "tool_result": {
      const tools = transcript.querySelectorAll(".tool");
      const tool = tools[tools.length - 1];
      if (tool) tool.querySelector(".tool-result").textContent = data.result;
      break;
    }
    case "approval_required":
      appendApproval(data);
      break;
    case "error":
      if (lastAssistant) lastAssistant.textContent += `\n[error] ${data.message}`;
      break;
    case "done":
      // Final usage could be displayed; transcript already has streamed text.
      break;
  }
  window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
}

loadAgents().then(() => promptEl.focus());
