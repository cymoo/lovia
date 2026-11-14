// api.js — a thin client for the lovia web API.
//
// This is the single place the bundled UI talks to the server, and it doubles
// as a reference implementation: import `api` (and `readSSE` for streaming) to
// build your own front-end against the same endpoints. Every method returns a
// Promise; `streamChat`/`reconnect` resolve to the raw `Response` so the caller
// controls how the SSE body is consumed (see `readSSE`). Behind a path prefix,
// or to react to a 401 in one place, call `configureApi` first.

const JSON_HEADERS = { 'content-type': 'application/json' };

let _base = '';
/** @type {((err: Error & { status?: number, code?: string }) => void) | null} */
let _onUnauthorized = null;
let _unauthorized = false;

/**
 * Point the client at a mounted server and route auth failures.
 *
 * `base` is the path prefix the server is served under ('' — the default — at
 * the root). `onUnauthorized` gets the first 401's error (see `apiError`) and
 * no later one, so a burst of failing calls prompts or redirects once; the
 * calls still reject as usual.
 * @param {{ base?: string, onUnauthorized?: (err: Error & { status?: number, code?: string }) => void }} [opts]
 */
export function configureApi({ base, onUnauthorized } = {}) {
  if (base !== undefined) _base = base.replace(/\/+$/, '');
  if (onUnauthorized !== undefined) _onUnauthorized = onUnauthorized;
}

const url = (path) => _base + path;

/** @returns {string} The mount prefix set by `configureApi` ('' at the root). */
export const basePath = () => _base;

/**
 * `fetch` for an app-relative path: the mount prefix added, a 401 routed.
 * @param {string} path
 * @param {RequestInit} [init]
 * @returns {Promise<Response>}
 */
export async function request(path, init) {
  const res = await fetch(url(path), init);
  if (res.status === 401 && _onUnauthorized && !_unauthorized) {
    _unauthorized = true;
    _onUnauthorized(await apiError(res.clone()));
  }
  return res;
}

/**
 * The Error for a failed response. API routes answer
 * `{detail: {code, message, hint?}}` (lovia/web/errors.py); FastAPI's own 422
 * (`detail` is a list) and a custom auth dependency (often a string) don't,
 * so every shape degrades to a readable message. Branch on `.code` or
 * `.status`, never on the message text.
 * @param {Response} res
 * @returns {Promise<Error>}
 */
export async function apiError(res) {
  const detail = (await res.json().catch(() => null))?.detail;
  let message = `${res.status} ${res.statusText}`;
  let code, hint;
  if (Array.isArray(detail)) {
    message = detail.map((e) => e?.msg ?? String(e)).join('; ') || message;
  } else if (detail && typeof detail === 'object') {
    ({ code, hint } = detail);
    message = detail.message || message;
  } else if (typeof detail === 'string' && detail) {
    message = detail;
  }
  // The hint rides in the message too: it's what a toast should show.
  return Object.assign(new Error(hint ? `${message} — ${hint}` : message), {
    status: res.status,
    code,
    hint,
  });
}

async function _json(res) {
  if (!res.ok) throw await apiError(res);
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

// Which directory the workspace routes (Files panel, uploads, raw bytes)
// serve: the open chat's (`session`) or a not-yet-sent draft's (`root`).
// workspaces.js sets it where chats work in directories of their own; until
// then every request serves the agent's own directory.
/** @type {() => { session?: string | null, root?: string | null }} */
let _scope = () => ({});
/** @param {() => { session?: string | null, root?: string | null }} fn */
export function setWorkspaceScope(fn) {
  _scope = fn;
}

/** @typedef {{ agent?: string, path?: string, download?: boolean }} RawOpts */
/** @param {RawOpts} [opts] */
const rawPath = ({ agent, path, download } = {}) =>
  `/api/workspace/raw${qs({ agent, path, download: download ? 1 : '', ..._scope() })}`;

export const api = {
  // ---- agents / server info ----
  listAgents: () => request('/api/agents').then(_json),
  getAgent: (name) => request(`/api/agents/${encodeURIComponent(name)}`).then(_json),
  info: () => request('/api/info').then(_json),

  // ---- chat ----
  // Non-streaming turn. `body`: { message, agent?, session_id? }.
  chat: (body) =>
    request('/api/chat', {
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
    request('/api/chat/stream', {
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
    request(`/api/chat/reconnect${qs({ session_id: sessionId })}`, {
      method: 'POST',
      headers: { accept: 'text/event-stream' },
      signal,
    }),
  // Resolve a pending approval. `body`: { session_id, call_id, decision,
  // remember?, scopes?, reason? } → { ok, remembered }. `scopes` narrows what
  // `remember` keeps to some of the request's own.
  approve: (body) =>
    request('/api/chat/approve', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  // The chat's remembered approvals: [{ scope, context, created_at, active }].
  listGrants: (sessionId) =>
    request(`/api/sessions/${encodeURIComponent(sessionId)}/grants`).then(_json),
  revokeGrant: (sessionId, scope, context) =>
    request(
      `/api/sessions/${encodeURIComponent(sessionId)}/grants${qs({ scope, context })}`,
      { method: 'DELETE' },
    ).then(_json),
  answer: (body) =>
    request('/api/chat/answer', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }),
  cancel: (sessionId) =>
    request(`/api/chat/cancel${qs({ session_id: sessionId })}`, { method: 'POST' }),
  // Queue a message into the active run. `body`: { session_id, message }.
  inject: (body) =>
    request('/api/chat/inject', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  // Withdraw a still-queued message. `body`: { session_id, id }.
  uninject: (body) =>
    request('/api/chat/uninject', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),

  // ---- sessions ----
  // `per_workspace`: page by folder — that many chats of each, plus every
  // pinned one (the sidebar's workspace groups). `archived`: the chats put
  // away, instead of those in use (a search covers both).
  /** @param {{ q?: string, limit?: number, parent?: string, per_workspace?: number, archived?: boolean }} [opts] */
  listSessions: ({ q = '', limit, parent, per_workspace, archived } = {}) =>
    request(
      `/api/sessions${qs({ q, limit, parent, per_workspace, archived: archived || undefined })}`,
    ).then(_json),
  // Currently-live background runs: [{ session_id, run_id, agent, status, turns }].
  listRuns: () => request('/api/runs').then(_json),
  // Persisted run records, newest first. `since` keeps only runs finished
  // after that timestamp — the missed-completion catch-up on page load.
  // `session_id` scopes to one chat — the context-ring restore on reload.
  // `run_id` picks one run's record — how a queued message's run ended.
  /** @param {{ session_id?: string, since?: number, limit?: number, run_id?: string }} [opts] */
  runHistory: ({ session_id, since, limit, run_id } = {}) =>
    request(`/api/runs/history${qs({ session_id, since, limit, run_id })}`).then(_json),
  getSession: (id) => request(`/api/sessions/${encodeURIComponent(id)}`).then(_json),
  renameSession: (id, title) =>
    request(`/api/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ title }),
    }).then(_json),
  // A chat's permission mode; applies from its next run.
  /** @param {string} id @param {string} workspace_mode */
  setSessionMode: (id, workspace_mode) =>
    request(`/api/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ workspace_mode }),
    }).then(_json),
  // Bind a chat from before chats had a directory to one, for good.
  /** @param {string} id @param {string} workspace_root */
  setSessionRoot: (id, workspace_root) =>
    request(`/api/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ workspace_root }),
    }).then(_json),
  /** @param {string[]} session_ids @param {string} root */
  associateSessions: (session_ids, root) =>
    request('/api/sessions/associate', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ session_ids, root }),
    }).then(_json),
  // ---- workspaces: the directories a new chat may pick ----
  listWorkspaces: () => request('/api/workspaces').then(_json),
  /** @param {string} path */
  addWorkspace: (path) =>
    request('/api/workspaces', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ path }),
    }).then(_json),
  // A listed directory's display name ('' restores the folder's own).
  /** @param {string} root @param {string} name */
  renameWorkspace: (root, name) =>
    request('/api/workspaces', {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ root, name }),
    }).then(_json),
  /** @param {string} id */
  removeWorkspace: (id) =>
    request(`/api/workspaces/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(_json),
  // Subdirectory paths completing `path` as typed — on this machine, or with
  // `host` (the front of a remote location's key, "ssh://box") on that one.
  /** @param {string} path @param {{ signal?: AbortSignal, host?: string }} [opts] */
  completeDir: (path, { signal, host } = {}) =>
    request(`/api/workspaces/dirs${qs({ path, host })}`, { signal }).then(_json),
  setPinned: (id, pinned) =>
    request(`/api/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ pinned }),
    }).then(_json),
  /** @param {string} id @param {boolean} archived */
  setArchived: (id, archived) =>
    request(`/api/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ archived }),
    }).then(_json),
  deleteSession: (id) =>
    request(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  deleteAllSessions: () => request('/api/sessions', { method: 'DELETE' }),
  getTodos: (id) =>
    request(`/api/sessions/${encodeURIComponent(id)}/todos`).then(_json),
  // Questions the user might ask next → { followups: string[] }. POST because
  // it spends model tokens; `[]` whenever the server has nothing to suggest.
  /** @param {string} id @param {{ signal?: AbortSignal }} [opts] */
  getFollowups: (id, { signal } = {}) =>
    request(`/api/sessions/${encodeURIComponent(id)}/followups`, {
      method: 'POST',
      signal,
    }).then(_json),
  // Rewind to just before the userTurn-th user message (edit / regenerate);
  // resolves to { removed, entries } — the authoritative post-rewind view.
  // `target` names the user message: `{ ref }`, or `{ user_turn }` for one
  // without a ref yet.
  rewindSession: (id, target) =>
    request(`/api/sessions/${encodeURIComponent(id)}/rewind`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(target),
    }).then(_json),
  // Copy the chat into a new one — up to just before the user message `ref`
  // names, or whole; resolves to { session_id } of the copy.
  /** @param {string} id @param {string | null} [ref] */
  forkSession: (id, ref = null) =>
    request(`/api/sessions/${encodeURIComponent(id)}/fork`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(ref ? { ref } : {}),
    }).then(_json),
  // The chat as a file → Response (`json` feeds the client-side HTML export).
  exportChat: (id, format = 'md') =>
    request(`/api/sessions/${encodeURIComponent(id)}/export${qs({ format })}`),
  // The lifecycle stream, for an `EventSource` (which can't use `request`).
  eventsUrl: () => url('/api/events'),

  // ---- schedules ----
  listSchedules: () => request('/api/schedules').then(_json),
  /** @param {string} id */
  getSchedule: (id) => request(`/api/schedules/${encodeURIComponent(id)}`).then(_json),
  // Create a scheduled run. `body`: { input, name?, agent?, session_id?,
  // workspace_root?, workspace_mode?, trigger_kind, trigger_expr, until?,
  // max_fires?, expires_at?, context? }.
  createSchedule: (body) =>
    request('/api/schedules', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  deleteSchedule: (id) =>
    request(`/api/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(
      _json,
    ),
  // Partial update: any subset of createSchedule's fields plus `active` — the
  // server revalidates and recomputes next_fire; explicit null clears a field.
  updateSchedule: (id, body) =>
    request(`/api/schedules/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  setScheduleActive: (id, active) =>
    request(`/api/schedules/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ active }),
    }).then(_json),
  // Fire a schedule immediately; 409 `schedule_not_fired` when it can't run now.
  runSchedule: (id) =>
    request(`/api/schedules/${encodeURIComponent(id)}/run`, { method: 'POST' }).then(
      _json,
    ),
  // A schedule's fire history, newest first: [{ run_id, session_id, status,
  // error, started_at, finished_at, usage }].
  /** @param {string} id @param {{ limit?: number }} [opts] */
  scheduleRuns: (id, { limit } = {}) =>
    request(`/api/schedules/${encodeURIComponent(id)}/runs${qs({ limit })}`).then(
      _json,
    ),
  // A trigger's next fires were it saved now → { next_fires, utc_offset };
  // rejects (422) with the reason when the trigger is invalid.
  /** @param {string} trigger_kind @param {string} trigger_expr */
  previewSchedule: (trigger_kind, trigger_expr) =>
    request('/api/schedules/preview', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ trigger_kind, trigger_expr }),
    }).then(_json),

  // ---- workspace (Files panel; read-only) ----
  /** @param {{ agent?: string }} [opts] */
  workspaceInfo: ({ agent } = {}) =>
    request(`/api/workspace${qs({ agent, ..._scope() })}`).then(_json),
  // One directory level, dirs first: { files, truncated } — `truncated` when
  // the workspace's listing cap left entries out. `path` is workspace-relative.
  /** @param {{ agent?: string, path?: string }} [opts] */
  workspaceFiles: ({ agent, path } = {}) =>
    request(`/api/workspace/files${qs({ agent, path, ..._scope() })}`).then(_json),
  // Every file under the root, root-relative and sorted (the agent's own
  // globbed listing: git's ignore rules, no dotfiles, no denied paths):
  // { paths, truncated }. Scoped like listSkills, so callers can key a cache.
  /** @param {{ session?: string, agent?: string, root?: string }} [where] */
  workspacePaths: (where = {}) => request(`/api/workspace/paths${qs(where)}`).then(_json),
  // The chat's own files (`paths`, as its tool calls spelled them), then —
  // in a git work tree — the other uncommitted changes: { git, truncated, files }.
  /** @param {{ agent?: string, paths?: string[] }} [opts] */
  workspaceChanges: ({ agent, paths = [] } = {}) =>
    request(`/api/workspace/changes${qs({ agent, ..._scope() })}`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ paths }),
    }).then(_json),
  // One file against HEAD: { path, diff, binary, truncated }. `old`: a
  // rename's former path, so git diffs the two instead of a new file.
  /** @param {{ agent?: string, path?: string, old?: string }} [opts] */
  workspaceDiff: ({ agent, path, old } = {}) =>
    request(`/api/workspace/diff${qs({ agent, path, old, ..._scope() })}`).then(_json),
  // Paginated text content; `binary: true` means "don't render me".
  /** @param {{ agent?: string, path?: string, start?: number }} [opts] */
  workspaceFile: ({ agent, path, start } = {}) =>
    request(`/api/workspace/file${qs({ agent, path, start, ..._scope() })}`).then(_json),
  // Raw bytes URL — inline image preview, or any file with download=true.
  /** @param {RawOpts} [opts] @returns {string} */
  workspaceRawUrl: (opts) => url(rawPath(opts)),
  // The same bytes as a Response, for code that reads them.
  /** @param {RawOpts} [opts] */
  workspaceRaw: (opts) => request(rawPath(opts)),
  // Bytes of one image a tool result carried — served from the transcript,
  // so it shows exactly what the model saw. `index` is 0-based over that
  // result's image parts (the SSE / history `images` stubs).
  /** @param {string} sessionId @param {string} callId @param {number} index @returns {string} */
  toolImageUrl: (sessionId, callId, index) =>
    url(
      `/api/sessions/${encodeURIComponent(sessionId)}/tool-images/${encodeURIComponent(callId)}/${index}`,
    ),
  // ---- background processes (chat-scoped; the panel's Processes strip) ----
  // Keyed by session id: processes belong to the chat's live workspace
  // session, not to the agent. `[]` when nothing ran yet (or after a server
  // restart — background processes never survive one).
  /** @param {string} sessionId */
  sessionProcesses: (sessionId) =>
    request(`/api/sessions/${encodeURIComponent(sessionId)}/processes`).then(_json),
  // Kill one process; resolves to the refreshed process list.
  /** @param {string} sessionId @param {string} processId */
  killProcess: (sessionId, processId) =>
    request(
      `/api/sessions/${encodeURIComponent(sessionId)}/processes/${encodeURIComponent(processId)}/kill`,
      { method: 'POST' },
    ).then(_json),

  // Upload a file into the workspace `uploads/` dir → { path, name, mime, kind,
  // size }. Multipart; the browser sets the boundary, so we send no headers.
  /** @param {File} file @param {{ agent?: string, signal?: AbortSignal }} [opts] */
  uploadFile: (file, { agent, signal } = {}) => {
    const form = new FormData();
    form.append('file', file);
    return request(`/api/workspace/upload${qs({ agent, ..._scope() })}`, {
      method: 'POST',
      body: form,
      signal,
    }).then(_json);
  },

  // ---- configuration (Settings → Models/Web search/Skills/Agents; the CLI's) ----
  // Only served when /api/info reports features.model_config. API keys never
  // round-trip: reads carry { set, hint }; writes use null=keep, ''=clear.
  getConfig: () => request('/api/config').then(_json),
  /** @param {object} body Profile fields: { id?, name?, model, flavor?, base_url?, api_key?, context_window?, max_tokens?, vision?, extra_body? }. */
  createModel: (body) =>
    request('/api/config/models', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  updateModel: (id, body) =>
    request(`/api/config/models/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  deleteModel: (id) =>
    request(`/api/config/models/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }).then(_json),
  // A server-side copy, API key included; resolves to { id, config }.
  duplicateModel: (id) =>
    request(`/api/config/models/${encodeURIComponent(id)}/duplicate`, {
      method: 'POST',
    }).then(_json),
  // Role assignment; sending { chat } switches the served model live.
  /** @param {{ chat?: string, vision?: string | null, aux?: string | null }} body */
  setRoles: (body) =>
    request('/api/config/roles', {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  /** @param {{ backend?: string, tavily_api_key?: string }} body */
  setSearch: (body) =>
    request('/api/config/search', {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  // Skill directories: full-list replacement, order = precedence.
  /** @param {{ dirs: string[] }} body */
  setSkills: (body) =>
    request('/api/config/skills', {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  // Settings → Agents. A body sends the fields to set (null = inherit, for
  // name/model/skills/max_retries); every write applies from the next run.
  // create/duplicate resolve to { id, config }, the rest to { config }.
  /** @param {object} body */
  createAgent: (body) =>
    request('/api/config/agents', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  /** @param {string} id @param {object} body */
  updateAgent: (id, body) =>
    request(`/api/config/agents/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  /** @param {string} id */
  deleteAgent: (id) =>
    request(`/api/config/agents/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }).then(_json),
  /** @param {string} id */
  duplicateAgent: (id) =>
    request(`/api/config/agents/${encodeURIComponent(id)}/duplicate`, {
      method: 'POST',
    }).then(_json),
  // The default agent is the first: this moves it there.
  /** @param {string} id */
  makeDefaultAgent: (id) =>
    request(`/api/config/agents/${encodeURIComponent(id)}/default`, {
      method: 'POST',
    }).then(_json),
  // Per-directory discovery report (live filesystem scan) for the pane.
  getSkills: () => request('/api/config/skills').then(_json),
  // Settings → MCP. `document` is the pasted mcpServers JSON (parsed); env
  // and header values come back masked, and a mask sent back unchanged keeps
  // what is stored. Each write resolves to the whole (masked) config.
  addMcpServers: (document) =>
    request('/api/config/mcp', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ document }),
    }).then(_json),
  /** @param {string} name @param {unknown} document */
  updateMcpServer: (name, document) =>
    request(`/api/config/mcp/${encodeURIComponent(name)}`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({ document }),
    }).then(_json),
  /** @param {string} name @param {boolean} enabled */
  setMcpServerEnabled: (name, enabled) =>
    request(`/api/config/mcp/${encodeURIComponent(name)}/enabled`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({ enabled }),
    }).then(_json),
  /** @param {string} name */
  deleteMcpServer: (name) =>
    request(`/api/config/mcp/${encodeURIComponent(name)}`, { method: 'DELETE' }).then(_json),
  // Connect, list tools, close: { name } for a saved server (its outcome is
  // remembered), or { document, original } for an edit not saved yet.
  // Resolves to { results: [{ name, ok, at, tools, error }] }.
  /** @param {{ name?: string, document?: unknown, original?: string | null }} body */
  testMcpServers: (body) =>
    request('/api/config/mcp/test', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),
  // Probe a connection (a real /models request server-side). Either free-form
  // fields or { profile_id } — the stored key is reused server-side, so the
  // page never holds it.
  testConnection: (body) =>
    request('/api/config/test', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(_json),

  // ---- skills ----
  // The skills a run of chat `session` sees — or of a draft opening in
  // `root` with `agent`: [{ name, description }], in the prompt index's order.
  /** @param {{ agent?: string, session?: string, root?: string }} [where] */
  listSkills: (where = {}) => request(`/api/skills${qs(where)}`).then(_json),

  // ---- memory (the agent's editable Notes) ----
  // Every call names a layer: `layer: 'user'` (the default) or 'project' —
  // the project of chat `session`, or of a draft's `root`, else the agent's own.
  /** @typedef {{ agent?: string, layer?: 'user' | 'project', session?: string, root?: string }} MemoryLayer */
  /** @param {MemoryLayer} [opts] */
  getMemory: (opts = {}) => request(`/api/memory${qs(opts)}`).then(_json),
  // Replaces the notes wholesale; returns the canonical stored form.
  /** @param {MemoryLayer & { content: string }} opts */
  putMemory: ({ content, ...where }) =>
    request(`/api/memory${qs(where)}`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({ content }),
    }).then(_json),
  // Runs the dream pass (merge duplicates, resolve conflicts, prune stale);
  // returns {before, after} counts plus the fresh canonical body.
  /** @param {MemoryLayer} [opts] */
  dreamMemory: (opts = {}) =>
    request(`/api/memory/dream${qs(opts)}`, { method: 'POST' }).then(_json),
  // Forgets a project — its notes and its conversation archive, for good.
  /** @param {MemoryLayer} opts */
  clearMemory: (opts) =>
    request(`/api/memory${qs({ ...opts, layer: 'project' })}`, { method: 'DELETE' }).then(_json),
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
