// api.js — a thin client for the lovia web API.
//
// This is the single place the bundled UI talks to the server, and it doubles
// as a reference implementation: import `api` (and `readSSE` for streaming) to
// build your own front-end against the same endpoints. Every method returns a
// Promise; `streamChat`/`reconnect` resolve to the raw `Response` so the caller
// controls how the SSE body is consumed (see `readSSE`).

const JSON_HEADERS = { 'content-type': 'application/json' };

async function _json(res) {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// Build a `?a=b&c=d` query string, skipping empty/nullish values.
function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') p.set(k, v);
  }
  const s = p.toString();
  return s ? `?${s}` : '';
}

export const api = {
  // ---- agents / server info ----
  listAgents: () => fetch('/api/agents').then(_json),
  getAgent: (name) => fetch(`/api/agents/${encodeURIComponent(name)}`).then(_json),
  info: () => fetch('/api/info').then(_json),
  renderMarkdown: (text) =>
    fetch('/api/markdown', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ text }),
    }).then(_json),

  // ---- chat ----
  // Non-streaming turn. `body`: { message, agent?, session_id? }.
  chat: (body) =>
    fetch('/api/chat', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  // Streaming turn → Response (consume with `readSSE`). `body` as above.
  streamChat: (body, { signal } = {}) =>
    fetch('/api/chat/stream', {
      method: 'POST',
      headers: { ...JSON_HEADERS, accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal,
    }),
  // Resume an interrupted run → Response (consume with `readSSE`).
  reconnect: (sessionId, { signal } = {}) =>
    fetch(`/api/chat/reconnect${qs({ session_id: sessionId })}`, {
      method: 'POST',
      headers: { accept: 'text/event-stream' },
      signal,
    }),
  // Resolve a pending approval. `body`: { session_id, call_id, decision }.
  approve: (body) =>
    fetch('/api/chat/approve', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }),
  cancel: (sessionId) =>
    fetch(`/api/chat/cancel${qs({ session_id: sessionId })}`, { method: 'POST' }),

  // ---- sessions ----
  listSessions: ({ q = '', limit } = {}) =>
    fetch(`/api/sessions${qs({ q, limit })}`).then(_json),
  getSession: (id) => fetch(`/api/sessions/${encodeURIComponent(id)}`).then(_json),
  renameSession: (id, title) =>
    fetch(`/api/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ title }),
    }).then(_json),
  deleteSession: (id) =>
    fetch(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  deleteAllSessions: () => fetch('/api/sessions', { method: 'DELETE' }),
  getTodos: (id) =>
    fetch(`/api/sessions/${encodeURIComponent(id)}/todos`).then(_json),
  exportUrl: (id, format = 'md') =>
    `/api/sessions/${encodeURIComponent(id)}/export${qs({ format })}`,
};

// Parse one SSE chunk ("event: x\ndata: y") into { event, data }, or null.
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

// Async-iterate the SSE events of a fetch Response:
//   for await (const { event, data } of readSSE(res)) { ... }
export async function* readSSE(response) {
  const reader = response.body.getReader();
  const dec = new TextDecoder();
  let raw = '';
  try {
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
          if (ev) yield ev;
        }
      }
      if (done) break;
    }
  } finally {
    try { reader.releaseLock(); } catch { /* already released */ }
  }
}
