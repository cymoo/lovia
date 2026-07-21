# HTTP API

The JSON + SSE API is decoupled from the bundled chat page — keep the
endpoints, drop the UI, and put your own front-end (or another service) on
top. Everything the bundled UI does goes through these routes; the full
interactive schema lives at `/api/docs` on any running server.

## Serving the API without the UI

Two depths. Turn the page off:

```python
from lovia.web import create_app

app = create_app(agent, ui=False)   # no GET / and no /static — API only
```

…or mount the router into your own FastAPI app (your middleware, your auth,
your lifecycle):

```python
from fastapi import FastAPI

from lovia.web import ChatStore, RouterDeps, build_api_router
from lovia.web.approvals import ApprovalRegistry

deps = RouterDeps(
    agents={"bot": agent},
    store=ChatStore.in_memory(),
    approvals=ApprovalRegistry(),
)
app = FastAPI()
app.include_router(build_api_router(deps))
```

`RouterDeps` is a plain dataclass — `agents`, `store`, and `approvals` are
required; run settings (`max_turns`, `budget`, `retry`, `tracer`,
`approval_timeout`, `max_background_runs`, title options) are fields with
the same defaults `create_app` uses.

## Authentication

With `create_app(token=...)` or `serve(token=...)`, every business route
registered by `build_api_router` requires authentication. A non-loopback
`serve()` bind also generates a token when no authentication was supplied.

Plain requests, `POST /api/chat/stream`, and `POST /api/chat/reconnect` send
`Authorization: Bearer <token>`. `GET /api/events` uses `EventSource`, which
cannot set custom headers, so the bundled UI authenticates it with the
`lovia_token` cookie. `GET /healthz` stays open. Missing or invalid credentials
return `401`; the `detail` names the *server token* so clients can distinguish
this from model-provider authentication failures.

Apps mounting `build_api_router` themselves must add their own dependency,
such as `token_dependency(token)` from `lovia.web.auth` or any FastAPI
authentication dependency. `/api/docs` and `/api/openapi.json` belong to the
FastAPI app rather than this router and remain public by default.

## Endpoints

| Method & path | Purpose |
| --- | --- |
| `GET /healthz` | liveness |
| `GET /api/info` | title, agents, default agent, version, feature flags |
| `GET /api/agents` · `GET /api/agents/{name}` | agent introspection (instructions, tools, capabilities) |
| `POST /api/chat` | one **blocking** Run → `{output, session_id, usage}` |
| `POST /api/chat/stream` | **SSE**: start a run, or attach to the session's live run (injecting the new message) |
| `POST /api/chat/reconnect?session_id=` | **SSE**: re-attach after refresh, or resume an interrupted checkpoint |
| `POST /api/chat/approve` | resolve a pending approval: `{session_id, call_id, decision}` |
| `POST /api/chat/cancel?session_id=` | stop the live run (completed turns are kept) |
| `POST /api/chat/inject` / `uninject` | queue / withdraw a [steering message](cancellation.md#steering-a-live-run) for the live run |
| `GET /api/sessions?q=&limit=&offset=` | list / search chats (pinned first, paged); `DELETE` clears all |
| `GET /api/runs` | live supervised runs |
| `GET /api/runs/history?session_id=&source=&since=&limit=&offset=` | persisted run records (outcome, error, duration, token usage); `since` filters to runs finished after that timestamp |
| `GET /api/events` | **SSE**: subscribe to process-wide lifecycle events (no history replay) |
| `GET` / `PATCH` / `DELETE /api/sessions/{id}` | transcript · rename/pin · delete |
| `GET /api/sessions/{id}/todos` | current [Todo list](todo.md), rebuilt from the Transcript |
| `POST /api/sessions/{id}/rewind` | drop everything from the user-message index `user_turn` onward (zero-based); 409 while a run is live, 501 if the store lacks `rewind` |
| `GET /api/sessions/{id}/export?format=md\|json\|txt` | export a chat |
| `GET` / `POST /api/schedules`, `GET` / `PATCH` / `DELETE /api/schedules/{id}`, `POST .../run` | [scheduled runs](web-server.md#scheduling): list, create, retime/pause, delete, fire now |
| `GET /api/schedules/{id}/runs` | a schedule's fire history (its run records, newest first) |
| `GET /api/workspace` · `/files` · `/recent` · `/file` · `/raw` | read-only file panel over the agent's [workspace](workspace.md) |
| `GET` / `PUT /api/memory?agent=` | read / replace the [Memory notes](memory.md#how-memories-get-written) (`{content, used, budget}`) |

### Lifecycle events

`GET /api/events` uses GET + `EventSource` to publish `run_started`,
`run_finished`, `session_created`, and `session_retitled`. It does not replay
history. On every connection or reconnection, fetch current state from
`/api/sessions` and `/api/runs` before processing new events. The server closes
subscribers that fall behind; recover them with the same snapshot-first flow.
To find Runs that finished while disconnected, query `/api/runs/history` with
`since`.

### Other behavior

- `/api/chat` returns 409 while a chat stream owns the Session.
- Starting another chat stream while that Session has a live Run attaches to
  the existing Run instead of starting a second one or failing.
- Workspace routes always access the workspace in read-only mode, regardless
  of the Agent's mode, and preserve its `denied_paths`. They hide regenerable
  files such as `__pycache__`, `*.pyc`, `venv`, and `node_modules`; dotfiles
  stay hidden too.

## Chat SSE streams

`POST /api/chat/stream` and `/api/chat/reconnect` return a `text/event-stream`
of `event:` / `data:` pairs: the Runner's
[typed events](streaming.md#event-catalog), with JSON-encoded data.

| SSE event | Payload |
| --- | --- |
| `session` | `{session_id}` — first frame of a new stream |
| `snapshot` | `{session_id, status, entries[]}` — re-attach prologue: the completed turns so far |
| `text_delta` / `reasoning_delta` | `{delta}` |
| `output_discarded` | `{}` — clear the current turn's rendered deltas |
| `message_completed` | `{message}` — one assistant turn, assembled |
| `user_injected` | `{content, turn}` |
| `tool_call` / `tool_result` | `{id, name, arguments}` / `{id, name, result, is_error}` |
| `todo` | `{call_id, todos: [...]}` — structured todo updates |
| `approval_required` | `{id, name, arguments}` → answer via `POST /api/chat/approve` |
| `handoff` / `turn_started` / `context_compacted` | transitions and [compaction notices](context.md) |
| `error` | `{type, message}` — tool-scoped, or terminal when the stream then ends |
| `done` | `{output, usage}` — terminal success |

Chat streams do not use Last-Event-Id. After a disconnect, POST
`/api/chat/reconnect` again to receive the latest `snapshot`, a replay of the
in-flight Turn (including any pending `approval_required`), and then live
events. The same recovery applies when the server disconnects a slow client.
Comment lines (`:`) are keep-alives and should be ignored.

## The bundled browser client

`lovia/web/static/js/api.js` is a dependency-free client for chat, Session,
scheduling, Workspace, Memory, and related endpoints. It also provides
`readSSE(response)`, an async generator over `{event, data}` pairs:

```js
import { api, readSSE } from "./api.js";

const res = await api.streamChat({ message: "hello" });
for await (const { event, data } of readSSE(res)) {
  if (event === "text_delta") render(data.delta);
}
```

Import it, or read it as the reference implementation for any language —
it is intentionally small.

## ChatStore

`ChatStore` is the storage bundle behind the API: a `Session` for transcripts,
a metadata table for `ChatMeta` rows (titles, timestamps, pins, and the
resumable `active_run_id`), a checkpointer, and schedule and run-record tables.
`ChatStore.sqlite(path, wal=False)` keeps everything in one file;
`ChatStore.in_memory()` is for tests and demos; `ChatStore(session=...,
meta_path=...)` wraps a custom `Session` backend while keeping the
metadata features.

## Sharp edges

- **`build_api_router` alone has no authentication or rate limits.**
  `create_app(token=...)` and `serve(token=...)` add token authentication;
  `serve()` also generates a token automatically for non-loopback binds
  ([Authentication](#authentication)). User identities, permissions, quotas,
  and other multi-user concerns belong in your gateway. `cors_origins` stays
  unset (no CORS) until configured.
- **Chat SSE responses are POST-initiated.** Use `fetch` + a reader for
  `/api/chat/stream` and `/api/chat/reconnect`; native `EventSource` will not
  work for them. `GET /api/events` is the `EventSource`-compatible stream.
- **`result` in `tool_result` is the raw value** (JSON-safe form) — the
  same duality as
  [`ToolCallCompleted`](streaming.md#tools-and-approval); render `result`
  for structure, fall back to strings.
- **Snapshots are per-turn, not per-token.** A re-attach mid-sentence
  replays that sentence's deltas from the turn buffer; your renderer must
  tolerate re-seeing deltas it already drew (idempotent rendering by turn,
  or just clear on `snapshot`).

## See also

- [Web server](web-server.md) — the server around these routes
- [Streaming](streaming.md) — the in-process form of the same events
- Example: [`27_web_api.py`](../../examples/27_web_api.py)
