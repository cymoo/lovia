# HTTP API

The bundled chat page and JSON + SSE API are independent. Disable the page when
you want to keep the endpoints and provide your own front end. A running server
exposes the interactive schema at `/api/docs`.

| Integration | Use it when |
| --- | --- |
| `create_app(agent, ui=False)` | You want a standalone service without the bundled page |
| `create_app(...)` | You want a complete ASGI app but will start the server yourself |
| `build_api_router(...)` | An existing FastAPI app already owns middleware and lifecycle |

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

deps = RouterDeps(agents={"bot": agent}, store=ChatStore.in_memory())
app = FastAPI(lifespan=deps.lifespan)
app.include_router(build_api_router(deps))
```

`RouterDeps` is a plain dataclass — only `agents` and `store` are required;
run settings (`max_turns`, `budget`, `retry`, `tracer`, `approval_timeout`,
`max_background_runs`, `scheduler_poll`, title options) are fields with the
same defaults `create_app` uses.

`deps.lifespan` runs the machinery behind the API: on startup it settles runs
a dead process left `running` and starts the schedule poller; on shutdown it
winds live runs down to resumable checkpoints and closes chat workspaces with
their background processes. Without it, schedules never fire. If your app
already has a lifespan, enter it inside yours:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # your own startup
    async with deps.lifespan():
        yield
    # your own shutdown

app = FastAPI(lifespan=lifespan)
```

Agents carrying the `Subagents` Plugin also need `wire_subagents(deps)`
once (see [Web UI](web-ui.md#background-subagents)).

## Authentication

With `create_app(token=...)` or `serve(token=...)`, every business route
registered by `build_api_router` requires authentication. A non-loopback
`serve()` bind also generates a token when no authentication was supplied.

Plain requests, `POST /api/chat/stream`, and `POST /api/chat/reconnect` send
`Authorization: Bearer <token>`. `GET /api/events` uses `EventSource`, which
cannot set custom headers, so the bundled UI authenticates it with the
`lovia_token` cookie. `GET /healthz` stays open. Missing or invalid credentials
return `401` with code `server_token` — distinct from a model provider
rejecting its API key, which arrives as a run error (`provider_auth`).

Apps mounting `build_api_router` themselves must add their own dependency,
such as `token_dependency(token)` from `lovia.web.auth` or any FastAPI
authentication dependency. `/api/docs` and `/api/openapi.json` belong to the
FastAPI app rather than this router and remain public by default.

## Errors

Every error an API route raises has one body:

```json
{"detail": {"code": "session_not_found", "message": "session not found", "hint": "..."}}
```

Branch on `code`, never on `message` wording; `hint`, when present, is a
suggested fix. Two exceptions keep FastAPI's own shapes: request validation
(422, `detail` is a list) and a custom `auth=` dependency (whatever it
raises). `apiError(response)` in the [bundled client](#the-bundled-browser-client)
normalizes all three.

| Error code | Status | Meaning |
| --- | --- | --- |
| `invalid_request` | 400 / 422 | a malformed or empty input the route rejected |
| `server_token` | 401 | missing or invalid server token |
| `local_origin_required` | 403 | a config write without auth from a non-local `Host` |
| `path_denied` | 403 | the workspace policy refuses the path |
| `agent_not_found` | 404 | no served agent by that name |
| `session_not_found` | 404 | no such chat |
| `schedule_not_found` | 404 | no such schedule |
| `model_not_found` | 404 | no such model profile |
| `file_not_found` | 404 | no such workspace file or directory |
| `turn_not_found` | 404 | rewind named a user turn the chat doesn't have |
| `image_not_found` | 404 | no servable image at that tool-result index |
| `approval_not_found` | 404 | no pending approval matches |
| `question_not_found` | 404 | no pending `ask_human` question matches |
| `process_not_found` | 404 | no such background process |
| `run_not_found` | 404 | no live or resumable run to cancel or reconnect to |
| `feature_unavailable` | 404 / 501 | the server or agent lacks it (no workspace, memory, checkpointer, question channel, or `rewind`) |
| `run_active` | 409 | a run owns this chat |
| `run_stopping` | 409 | a stopped run is still winding down; retry shortly |
| `agent_unregistered` | 409 | the interrupted run's agent is no longer served |
| `schedule_not_fired` | 409 | the schedule couldn't fire right now |
| `model_exists` | 409 | a model profile with that id exists |
| `model_in_use` | 409 | the default chat model can't be deleted |
| `file_too_large` | 413 | over the workspace read or upload limit |
| `unsupported_file_type` | 415 | not previewable inline, or an extension uploads don't allow |
| `too_many_runs` | 429 | at the concurrent-run cap |

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
| `POST /api/sessions/{id}/followups` | `{followups: [...]}` — questions the user might ask next; `[]` when the feature is off or nothing fits. POST because it spends model tokens |
| `POST /api/sessions/{id}/rewind` | drop everything from the user-message index `user_turn` onward (zero-based); 409 while a run is live, 501 if the store lacks `rewind` |
| `GET /api/sessions/{id}/export?format=md\|json\|txt` | export a chat |
| `GET` / `POST /api/schedules`, `GET` / `PATCH` / `DELETE /api/schedules/{id}`, `POST .../run` | [scheduled runs](web-server.md#scheduling): list, create, retime/pause, delete, fire now |
| `GET /api/schedules/{id}/runs` | a schedule's fire history (its run records, newest first) |
| `GET /api/workspace` · `/files` · `/recent` · `/file` · `/raw` | read-only file panel over the agent's [workspace](workspace.md) |
| `GET` / `PUT /api/memory?agent=` | read / replace the [Memory notes](memory.md#how-memories-get-written) (`{content, used, budget}`) |
| `GET /api/config` · `POST /api/config/models` · `PUT`/`DELETE /api/config/models/{id}` · `PUT /api/config/roles` · `PUT /api/config/search` · `PUT`/`GET /api/config/skills` · `POST /api/config/test` | the CLI default agent's [model configuration](web-ui.md#models-and-switching) and [skill directories](web-ui.md#skills) — present only when `/api/info` reports `features.model_config`. Keys are write-only (`null` keeps, `""` clears; reads carry `{set, hint}`); writes validate the whole document, persist it, and hot-swap the served agent. `GET /api/config/skills` reports per-directory discovery |

### Lifecycle events

`GET /api/events` uses GET + `EventSource` to publish:

| Lifecycle event | Payload |
| --- | --- |
| `run_started` | `{session_id, run_id, agent, source}` |
| `run_finished` | `{session_id, run_id, status, error, source}` |
| `session_created` | `{session_id, agent, title}` |
| `session_retitled` | `{session_id, title}` |
| `config_changed` | `{configured, model, profile_id, name}` — refetch `/api/config` |

It does not replay history. On every connection or reconnection, fetch current state from
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

`?` marks a field that may be absent.

| SSE event | Payload |
| --- | --- |
| `session` | `{session_id}` — first frame of a new stream |
| `snapshot` | `{session_id, status, entries}` — re-attach prologue: the completed turns so far |
| `text_delta` | `{delta}` |
| `reasoning_delta` | `{delta}` |
| `output_discarded` | `{}` — clear the current turn's rendered deltas |
| `message_completed` | `{message}` — one assistant turn, assembled |
| `user_injected` | `{content, turn}` |
| `tool_call` | `{id, name, arguments}` — `arguments` is the raw JSON string |
| `tool_result` | `{id, name, result, is_error, images?}` — `images` lists `{index, mime_type}` stubs served by `GET /api/sessions/{id}/tool-images/{call_id}/{index}` |
| `todo` | `{call_id, name, todos}` — a structured todo update, in place of that call's `tool_result` |
| `approval_required` | `{id, name, arguments}` → answer via `POST /api/chat/approve` |
| `handoff` | `{from, to}` |
| `turn_started` | `{turn, agent}` |
| `context_compacted` | `{session_id, reason, reactive, summary, tokens_before, tokens_after, detail}` — a [compaction notice](context.md) |
| `error` | `{type, message, code, status_code?, retryable?, hint?}` — tool-scoped, or terminal when the stream then ends |
| `done` | `{output, usage}` — terminal success |

An `error` event's `code` classifies the failure; `status_code` and
`retryable` come from a provider error, `hint` from any lovia error carrying
one:

| Run error code | Meaning |
| --- | --- |
| `tool_error` | a tool call failed; the model sees the error and usually carries on |
| `provider_auth` | the provider rejected the credentials (401 / 403) |
| `rate_limited` | the provider rate-limited the request (429) |
| `overloaded` | the provider is overloaded (503 / 529) |
| `timeout` | the provider request timed out |
| `network` | the connection to the provider failed |
| `provider_error` | any other provider failure |
| `context_overflow` | the prompt exceeds the model's context window |
| `budget_exceeded` | a `RunBudget` limit tripped |
| `max_turns` | the run hit `max_turns` without finishing |
| `cancelled` | the run was cancelled |
| `guardrail` | a guardrail rejected the input or output |
| `output_invalid` | the output didn't parse into the agent's `output_type` |
| `internal` | anything else |

Chat streams do not use Last-Event-Id. After a disconnect, POST
`/api/chat/reconnect` again to receive the latest `snapshot`, a replay of the
in-flight Turn (including any pending `approval_required`), and then live
events. The same recovery applies when the server disconnects a slow client.
Comment lines (`:`) are keep-alives and should be ignored.

## The bundled browser client

`lovia/web/static/js/api.js` is a dependency-free client for chat, Session,
scheduling, Workspace, Memory, and related endpoints. Its methods reject with
an `Error` carrying `status`, `code`, and `hint` (built by `apiError(response)`,
exported for your own `fetch` calls). It also provides `readSSE(response)`, an
async generator over `{event, data}` pairs:

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
`ChatStore.sqlite(path, wal=True)` keeps everything in one file, in WAL mode so
the stores sharing it do not queue behind each other (see
[Web server](web-server.md));
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
