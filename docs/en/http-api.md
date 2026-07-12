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

## Endpoints

| Method & path | Purpose |
| --- | --- |
| `GET /healthz` | liveness |
| `GET /api/info` | title, agents, default agent, version, feature flags |
| `GET /api/agents` · `GET /api/agents/{name}` | agent introspection (instructions, tools, capabilities) |
| `POST /api/chat` | one **blocking** turn → `{output, session_id, usage}` |
| `POST /api/chat/stream` | **SSE**: start a run, or attach to the session's live run (injecting the new message) |
| `POST /api/chat/reconnect?session_id=` | **SSE**: re-attach after refresh, or resume an interrupted checkpoint |
| `POST /api/chat/approve` | resolve a pending approval: `{session_id, call_id, decision}` |
| `POST /api/chat/cancel?session_id=` | stop the live run (completed turns are kept) |
| `POST /api/chat/inject` / `uninject` | queue / withdraw a [steering message](reliability.md#steering-a-live-run) for the live run |
| `GET /api/sessions?q=&limit=` | list / search chats (pinned first); `DELETE` clears all |
| `GET /api/runs` | live supervised runs |
| `GET` / `PATCH` / `DELETE /api/sessions/{id}` | transcript · rename/pin · delete |
| `GET /api/sessions/{id}/todos` | current [Todo list](todo.md), rebuilt from the Transcript |
| `GET /api/sessions/{id}/export?format=md\|json\|txt` | export a chat |
| `GET` / `POST /api/schedules`, `GET` / `PATCH` / `DELETE /api/schedules/{id}`, `POST .../run` | [scheduled runs](web.md#scheduling): list, create, retime/pause, delete, fire now |
| `GET /api/workspace` · `/files` · `/recent` · `/file` · `/raw` | read-only file panel over the agent's [workspace](workspace.md) |
| `GET` / `PUT /api/memory?agent=` | read / replace the [Memory notes](memory.md#how-memories-get-written) (`{content, used, budget}`) |

Semantics worth knowing: `/api/chat` returns 409 while a stream owns the
session; starting a second stream on a live session *attaches* instead of
erroring; workspace routes run through a forced-readonly session (the
agent's `denied_paths` carried over) regardless of the agent's own mode,
and hide regenerable environment junk (`__pycache__`, `*.pyc`, `venv`,
`node_modules` — dotfiles were already hidden) so `/recent` stays about
the user's actual files.

## The SSE stream

`POST /api/chat/stream` (and `/reconnect`) answer with an `text/event-stream`
of `event:` / `data:` pairs — the runner's
[typed events](streaming.md#event-catalog), JSON-encoded:

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

The reconnect contract is deliberately simple: there is no Last-Event-Id
bookkeeping. A client that loses the connection (or whose subscriber queue
overflowed — the server closes slow consumers) just re-POSTs
`/api/chat/reconnect` and receives a fresh authoritative `snapshot`, a
replay of the in-flight turn's events (a still-pending
`approval_required` included), then the live tail. Comment lines (`:`) are
keep-alives — skip them.

## The bundled browser client

`lovia/web/static/js/api.js` is a dependency-free client covering every
endpoint (`api.chat`, `api.streamChat`, `api.reconnect`, `api.approve`,
sessions, schedules, workspace, memory) plus `readSSE(response)` — an async
generator over `{event, data}` pairs:

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

`ChatStore` is the storage bundle behind the API: a `Session` (transcripts)
plus a metadata table (`ChatMeta` rows — titles, timestamps, pins, the
resumable `active_run_id`) plus a checkpointer and the schedules table.
`ChatStore.sqlite(path, wal=False)` keeps everything in one file;
`ChatStore.in_memory()` is for tests and demos; `ChatStore(session=...,
meta_path=...)` wraps a custom `Session` backend while keeping the
metadata features.

## Sharp edges

- **No auth, no rate limits** — this is a component, not a product edge.
  Mount it behind your gateway; `cors_origins` stays unset (no CORS) until
  you say otherwise.
- **SSE responses are POST-initiated**, not `EventSource`-compatible GETs.
  Use `fetch` + a reader (as `api.js` does); native `EventSource` won't
  work.
- **`result` in `tool_result` is the raw value** (JSON-safe form) — the
  same duality as
  [`ToolCallCompleted`](streaming.md#tools-and-approval); render `result`
  for structure, fall back to strings.
- **Snapshots are per-turn, not per-token.** A re-attach mid-sentence
  replays that sentence's deltas from the turn buffer; your renderer must
  tolerate re-seeing deltas it already drew (idempotent rendering by turn,
  or just clear on `snapshot`).

## See also

- [Web UI & server](web.md) — the server around these routes
- [Streaming](streaming.md) — the in-process form of the same events
- Example: [`27_web_api.py`](../../examples/27_web_api.py)
