// api.js — a thin client for the lovia web API.
//
// This is the single place the bundled UI talks to the server, and it doubles
// as a reference implementation: import `api` (and `readSSE` for streaming) to
// build your own front-end against the same endpoints. Every method returns a
// Promise; `streamChat`/`reconnect` resolve to the raw `Response` so the caller
// controls how the SSE body is consumed (see `readSSE`).

const JSON_HEADERS = { 'content-type': 'application/json' };

async function _json(res) {
  if (!res.ok) {
    const err = new Error(`${res.status} ${res.statusText}`);
    err.status = res.status; // callers branch on 401 (token prompt)
    throw err;
  }
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

  // ---- chat ----
  // Non-streaming turn. `body`: { message, agent?, session_id? }.
  chat: (body) =>
    fetch('/api/chat', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  // Streaming turn → Response (consume with `readSSE`). `body` as above.
  /**
   * @param {object} body Chat request: `{ message, agent?, session_id? }`.
   * @param {{ signal?: AbortSignal }} [opts]
   * @returns {Promise<Response>} SSE stream — consume with `readSSE`.
   */
  streamChat: (body, { signal } = {}) =>
    fetch('/api/chat/stream', {
      method: 'POST',
      headers: { ...JSON_HEADERS, accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal,
    }),
  // Resume an interrupted run → Response (consume with `readSSE`).
  /**
   * @param {string} sessionId
   * @param {{ signal?: AbortSignal }} [opts]
   * @returns {Promise<Response>} SSE stream — consume with `readSSE`.
   */
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
  // Queue a message into the active run. `body`: { session_id, message }.
  inject: (body) =>
    fetch('/api/chat/inject', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  // Withdraw a still-queued message. `body`: { session_id, id }.
  uninject: (body) =>
    fetch('/api/chat/uninject', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),

  // ---- sessions ----
  /** @param {{ q?: string, limit?: number, offset?: number }} [opts] */
  listSessions: ({ q = '', limit, offset } = {}) =>
    fetch(`/api/sessions${qs({ q, limit, offset })}`).then(_json),
  // Currently-live background runs: [{ session_id, run_id, agent, status, turns }].
  listRuns: () => fetch('/api/runs').then(_json),
  // Persisted run records, newest first. `since` keeps only runs finished
  // after that timestamp — the missed-completion catch-up on page load.
  // `session_id` scopes to one chat — the context-ring restore on reload.
  /** @param {{ session_id?: string, since?: number, limit?: number }} [opts] */
  runHistory: ({ session_id, since, limit } = {}) =>
    fetch(`/api/runs/history${qs({ session_id, since, limit })}`).then(_json),
  getSession: (id) => fetch(`/api/sessions/${encodeURIComponent(id)}`).then(_json),
  renameSession: (id, title) =>
    fetch(`/api/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ title }),
    }).then(_json),
  setPinned: (id, pinned) =>
    fetch(`/api/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ pinned }),
    }).then(_json),
  deleteSession: (id) =>
    fetch(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  deleteAllSessions: () => fetch('/api/sessions', { method: 'DELETE' }),
  getTodos: (id) =>
    fetch(`/api/sessions/${encodeURIComponent(id)}/todos`).then(_json),
  // Rewind to just before the userTurn-th user message (edit / regenerate);
  // resolves to { removed, entries } — the authoritative post-rewind view.
  rewindSession: (id, userTurn) =>
    fetch(`/api/sessions/${encodeURIComponent(id)}/rewind`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ user_turn: userTurn }),
    }).then(_jsonOrDetail),
  exportUrl: (id, format = 'md') =>
    `/api/sessions/${encodeURIComponent(id)}/export${qs({ format })}`,

  // ---- schedules ----
  listSchedules: () => fetch('/api/schedules').then(_json),
  // Create a scheduled run. `body`: { input, agent?, session_id?, trigger_kind,
  // trigger_expr, until?, max_fires?, expires_at? }. Surfaces the server's
  // validation `detail` on 4xx.
  createSchedule: (body) =>
    fetch('/api/schedules', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_jsonOrDetail),
  deleteSchedule: (id) =>
    fetch(`/api/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(
      _jsonOrDetail,
    ),
  // Partial update: any subset of { input, agent, session_id, trigger_kind,
  // trigger_expr, active, until, max_fires, expires_at } — the server
  // revalidates and recomputes next_fire; explicit null clears a field.
  updateSchedule: (id, body) =>
    fetch(`/api/schedules/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_jsonOrDetail),
  setScheduleActive: (id, active) =>
    fetch(`/api/schedules/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ active }),
    }).then(_jsonOrDetail),
  // Fire a schedule immediately; 409 (with detail) when it can't run now.
  runSchedule: (id) =>
    fetch(`/api/schedules/${encodeURIComponent(id)}/run`, { method: 'POST' }).then(
      _jsonOrDetail,
    ),
  // A schedule's fire history, newest first: [{ run_id, session_id, status,
  // error, started_at, finished_at, usage }].
  /** @param {string} id @param {{ limit?: number }} [opts] */
  scheduleRuns: (id, { limit } = {}) =>
    fetch(`/api/schedules/${encodeURIComponent(id)}/runs${qs({ limit })}`).then(
      _json,
    ),

  // ---- workspace (Files panel; read-only) ----
  /** @param {{ agent?: string }} [opts] */
  workspaceInfo: ({ agent } = {}) =>
    fetch(`/api/workspace${qs({ agent })}`).then(_jsonOrDetail),
  // One directory level, dirs first. `path` is workspace-relative.
  /** @param {{ agent?: string, path?: string }} [opts] */
  workspaceFiles: ({ agent, path } = {}) =>
    fetch(`/api/workspace/files${qs({ agent, path })}`).then(_jsonOrDetail),
  // Whole-workspace flat list, newest first.
  /** @param {{ agent?: string, limit?: number }} [opts] */
  workspaceRecent: ({ agent, limit } = {}) =>
    fetch(`/api/workspace/recent${qs({ agent, limit })}`).then(_jsonOrDetail),
  // Paginated text content; `binary: true` means "don't render me".
  /** @param {{ agent?: string, path?: string, start?: number }} [opts] */
  workspaceFile: ({ agent, path, start } = {}) =>
    fetch(`/api/workspace/file${qs({ agent, path, start })}`).then(_jsonOrDetail),
  // Raw bytes URL — inline image preview, or any file with download=true.
  /** @param {{ agent?: string, path?: string, download?: boolean }} [opts] @returns {string} */
  workspaceRawUrl: ({ agent, path, download } = {}) =>
    `/api/workspace/raw${qs({ agent, path, download: download ? 1 : '' })}`,
  // Upload a file into the workspace `uploads/` dir → { path, name, mime, kind,
  // size }. Multipart; the browser sets the boundary, so we send no headers.
  /** @param {File} file @param {{ agent?: string, signal?: AbortSignal }} [opts] */
  uploadFile: (file, { agent, signal } = {}) => {
    const form = new FormData();
    form.append('file', file);
    return fetch(`/api/workspace/upload${qs({ agent })}`, {
      method: 'POST',
      body: form,
      signal,
    }).then(_jsonOrDetail);
  },

  // ---- memory (the agent's editable Notes) ----
  /** @param {{ agent?: string }} [opts] */
  getMemory: ({ agent } = {}) => fetch(`/api/memory${qs({ agent })}`).then(_jsonOrDetail),
  // Replaces the notes wholesale; returns the canonical stored form.
  putMemory: ({ agent, content }) =>
    fetch(`/api/memory${qs({ agent })}`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({ content }),
    }).then(_jsonOrDetail),
};

// Like `_json`, but raises the server's `{detail}` message (422/404) so the
// schedule form can show *why* a trigger was rejected.
async function _jsonOrDetail(res) {
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const err = new Error(body?.detail || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

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

/**
 * Async-iterate the SSE events of a fetch Response:
 *   for await (const { event, data } of readSSE(res)) { ... }
 * @param {Response} response Streaming response (from `streamChat`/`reconnect`).
 * @returns {AsyncGenerator<{ event: string, data: any }>}
 */
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
