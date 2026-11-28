# HTTP API

The bundled chat page and JSON + SSE API are independent. Disable the page when
you want to keep the endpoints and provide your own front end. A running server
exposes the interactive schema at `/api/docs`.

| Integration | Use it when |
| --- | --- |
| `create_app(agent, ui=False)` | You want a standalone service without the bundled page |
| `create_app(...)` | You want a complete ASGI app but will start the server yourself |
| `build_api_router(...)` | An existing FastAPI app already owns middleware and lifecycle |
| `app.mount(prefix, create_app(...))` | An existing app should host a complete lovia app under a prefix |

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
run settings (`max_turns`, `budget`, `retry`, `tracer`, `suspend_after`,
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

### Mounting a `create_app()` app

An app built by `create_app()` can also be mounted under a prefix of yours.
Starlette runs only the outer app's lifespan — a mounted app's never starts,
and nothing reports it — so enter it from yours:

```python
lovia_app = create_app(agent)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with lovia_app.state.deps.lifespan():
        yield

app = FastAPI(lifespan=lifespan)
app.mount("/lovia", lovia_app)            # page at /lovia/, API at /lovia/api/...
```

## Authentication

With `create_app(token=...)`, every business route registered by
`build_api_router` requires authentication. `serve(agent)` on a non-loopback
host generates one when none was supplied.

Plain requests, `POST /api/chat/stream`, and `POST /api/chat/reconnect` send
`Authorization: Bearer <token>`. `GET /api/events` uses `EventSource`, which
cannot set custom headers, so the bundled UI authenticates it with the
`lovia_token` cookie. `GET /healthz` is served by `create_app` outside the
guard, so it stays open under `token` and `auth` alike. Missing or invalid credentials
return `401` with code `server_token` — distinct from a model provider
rejecting its API key, which arrives as a run error (`provider_auth`).

Apps mounting `build_api_router` themselves must add their own dependency,
such as `token_dependency(token)` from `lovia.web.auth` or any FastAPI
authentication dependency — and their own health probe, since `/healthz` is
not part of the router. `/api/docs` and `/api/openapi.json` belong to the
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
| `mcp_server_not_found` | 404 | no MCP server by that name in Settings |
| `file_not_found` | 404 | no such workspace file or directory |
| `turn_not_found` | 404 | rewind or fork named a user turn the chat doesn't have |
| `image_not_found` | 404 | no servable image at that tool-result index |
| `ui_data_not_found` | 404 | no component data under that ref in this chat |
| `approval_not_found` | 404 | no pending approval matches |
| `question_not_found` | 404 | no pending `ask_human` question matches |
| `process_not_found` | 404 | no such background process |
| `run_not_found` | 404 | no live or resumable run to cancel or reconnect to |
| `feature_unavailable` | 404 / 501 | the server or agent lacks it (no workspace, memory, checkpointer, question channel, or `rewind`) |
| `run_active` | 409 | a run owns this chat |
| `run_stopping` | 409 | a stopped run is still winding down; retry shortly |
| `run_waiting` | 409 | the chat's run is suspended on an approval: answer its card to resume it, or stop it |
| `run_interrupted` | 409 | the chat's run stopped at a limit or on a failure: a message to the chat continues it (refused by reconnect, and for a stream sent with `busy="reject"`) |
| `run_unfinished` | 409 | the chat's last run was interrupted and can still resume; a whole-chat fork waits for it |
| `nothing_to_fork` | 409 | the fork would be empty: no finished history before that point |
| `run_cancelled` | 409 | a blocking turn's run was stopped (cancel, chat deletion, or shutdown) |
| `agent_unregistered` | 409 | the interrupted run's agent is no longer served |
| `schedule_not_fired` | 409 | the schedule couldn't fire right now |
| `model_exists` | 409 | a model profile with that id exists |
| `mcp_server_exists` | 409 | an MCP server with that name exists |
| `model_in_use` | 409 | the default chat model can't be deleted |
| `config_read_only` | 409 | `config.json` is not writable (e.g. a read-only mount) |
| `workspace_locked` | 403 | the workspace settings are fixed by how `lovia web` was started |
| `workspace_not_allowed` | 409 | this server doesn't let a chat work in that directory (`hint` names the constraint); a chat is refused, never moved |
| `workspace_missing` | 409 | the chat's directory no longer exists |
| `workspace_unassociated` | 409 | a chat from before chats had a directory: bind it to one first (`POST /api/sessions/associate`) |
| `workspace_fixed` | 409 | the chat already works in a directory, which never changes |
| `workspace_not_found` | 404 | no such entry in the workspace list (the fixed ones can't be removed) |
| `workspace_unreachable` | 502 | a remote location's host could not be reached — to look the directory up, or to read or write its files (`hint` carries what it said) |
| `workspace_io_error` | 500 | the workspace's system could not do a read or write — a full disk, say; the message is what it said. A missing OS permission is 403 `path_denied` |
| `attachment_io_error` | 500 | an upload could not be saved in the chat's attachment folder — a full disk, say; the message is what the system said |
| `launch_failed` | 502 | the system's file manager or editor launcher refused (`open`, `xdg-open`); the message is what it said |
| `file_too_large` | 413 | over the workspace read or upload limit |
| `unsupported_file_type` | 415 | not previewable inline, or an extension uploads don't allow |
| `too_many_runs` | 429 | at the concurrent-run cap |
| `run_failed` | 500 | a blocking turn's run failed; `message` is its error |
| `transcription_failed` | 502 | the speech-to-text service refused a take, timed out or could not be reached; `message` says which, with its status and words. Never the service's own status: its 401 is about its key, not the caller's |

## Endpoints

| Method & path | Purpose |
| --- | --- |
| `GET /healthz` | liveness (served by `create_app`, not `build_api_router`) |
| `GET /api/info` | title, agents, default agent, version, feature flags, `workspace_modes: {modes, default}` when chats can pick a permission mode (`create_app(workspaces=)`; else `null`), and `desktop: {reveal, edit}` when the server can show files on its own desktop (`reveal`: `finder`, `explorer` or `folder`; else `null`). `take_seconds` is the longest voice-input take the transcription service accepts, where the server knows it (else `null`) |
| `GET /api/agents` · `GET /api/agents/{name}` | agent introspection (instructions, tools, capabilities — `workspace`, `vision`: whether its model sees images) |
| `POST /api/chat` | one **blocking** turn → `{output, session_id, usage}`: a supervised Run, waited for ([details](#blocking-turns)) |
| `POST /api/chat/stream` | **SSE**: start a run, or attach to the session's live run (injecting the new message). A chat whose last run was interrupted (a limit, a failure, a shutdown) continues that run instead, the message read at its next turn and its limits counted afresh; `POST /api/chat` does the same |
| `POST /api/chat/reconnect?session_id=` | **SSE**: re-attach after refresh, or resume an interrupted checkpoint |
| `POST /api/chat/approve` | resolve a pending approval: `{session_id, call_id, decision, remember?, scopes?, reason?}` → `{ok, remembered, resumed}`. Answering a run suspended on that request (`suspended`) resumes it with the answer, with no client attached (`resumed: true`): attach with `POST /api/chat/reconnect`. `POST /api/chat/answer` does the same for a suspended `ask_human` question (`{ok, resumed}`). `remember` (approve only) also saves an "always allow" grant for each of the request's own `scopes` in this chat — or only for the ones listed in `scopes`, which may narrow but never widen them (one the request does not offer → `400 invalid_request`, and nothing is decided); `reason` (deny only) reaches the model. The grants are written before the run wakes, so a repeat call in those scopes never asks meanwhile; `remembered` is false when nothing was picked, the request had no scopes, or saving failed (the approval still stands) |
| `POST /api/chat/cancel?session_id=` | stop the live run (completed turns are kept) |
| `POST /api/chat/inject` / `uninject` | queue / withdraw a [steering message](cancellation.md#steering-a-live-run) for the live run |
| `GET /api/sessions?q=&limit=&offset=&per_workspace=&archived=` | list / search chats (pinned first, paged). `per_workspace=N` pages by directory instead: every pinned chat, plus the `N` most recent of each directory, so a busy one can't crowd the others out (chats with no directory all come). Archived chats are left out; `archived=true` lists those instead, and a search (`q`) covers both — each row says which with `archived`. `DELETE` clears all |
| `GET /api/runs` | live supervised runs, then those suspended on an approval (`status: "waiting"`, holding no slot). A run's `run_id` is the one its lifecycle events and history record carry, kept across auto-chained legs and a resume. `pending_approval` is the request a `blocked_on_approval` or `waiting` run waits on, shaped like its `approval_required` frame (`null` otherwise) — so a page can offer it outside that run's chat, as the web UI does for a chat's subagent tasks; answer it with `POST /api/chat/approve` and the run's `session_id`. `waits_on` (`{kind, id}`, `null` otherwise) names what the run waits on a person for — an `approval` card, an ask_human `question`, or another tool's wait (`call`) — and the call's id: what a page tells the user about, once per call. `step` (`{name, args}`, `null` between calls) is the tool call under way, with only its short plain arguments: enough for one line |
| `GET /api/sessions?parent={id}` | the chat's subagent tasks. Each row also has `spawn_id` (what its `spawn_subagent` result names: `… [spawn <id>] …`), `last_run_status` and `last_run_seconds` (how long the latest run took, once over) |
| `GET /api/sessions/{id}/summary` | the chat in short — a task's detail without its transcript → `{prompt, step, report, error}`: what it was first asked, the tool call under way, the last answer once no run is live, and why the latest run failed. 404 `session_not_found` |
| `GET /api/runs/history?session_id=&source=&since=&run_id=&limit=&offset=` | persisted run records (outcome, error and its `error_code`, duration, token usage); `since` filters to runs finished after that timestamp, `run_id` picks one run |
| `GET /api/events` | **SSE**: subscribe to process-wide lifecycle events (no history replay) |
| `GET` / `PATCH` / `DELETE /api/sessions/{id}` | transcript · rename/pin/archive/switch `workspace_mode` · delete (with its subagent tasks and the schedules bound to it). `{"archived": true}` puts a chat away without deleting anything (and unpins it); pinning it, or a message from the user, brings it back. Each user message carries a `ref` — `"<run_id>:<n>"`, stable across reloads and rewinds; `null` until its run has finished. The answer that ends a run carries its `duration` in seconds, and timestamps are real where the run recorded its timing. A chat that came from another carries `origin` — `kind` is `"fork"` (a branch) or `"spawn"` (started by that chat's agent with `start_session`), or `"schedule"` for a chat a schedule's fire created (`schedule_id` instead of `session_id`; `title` is the schedule's name) — and the list's `origin_kind` says the same. An id the store doesn't have (from another database, or deleted) still answers, with an empty placeholder and `exists: false`; a first message with it starts a new chat. `last_error` (`{message, code}`, `null` otherwise) is the error of the chat's last run when it failed for good and nothing ran since — what its `error` event said (`code` a run error code, below), for a page opened afterwards |
| `GET /api/sessions/{id}/todos` | current [Todo list](todo.md), rebuilt from the Transcript |
| `POST /api/sessions/{id}/followups` | `{followups: [...]}` — questions the user might ask next; `[]` when the feature is off or nothing fits. POST because it spends model tokens |
| `POST /api/transcribe` | a WAV file as the raw body, `Content-Type: audio/wav` → `{text}`: one recorded take as text, for voice input (experimental, see [Stability](stability.md#not-covered)). Up to 10 MiB, checked as it arrives; nothing is kept. 404 `feature_unavailable` when the server has no transcriber (`/api/info`'s `features.transcription`), 415 for another media type, 413 past the limit, 422 for an empty body, 502 `transcription_failed` when the service fails |
| `POST /api/sessions/{id}/rewind` | drop everything from a user message onward, named by its `ref` or by `user_turn`, its zero-based index among the user messages; 404 for a `ref` no longer in the chat, 409 while a run is live, 501 if the store lacks `rewind` |
| `POST /api/sessions/{id}/fork` | `{ref?}` → `{session_id}`: copy the chat's finished history into a new, independent chat — up to just before the user message `ref` names, or whole. The new chat's detail carries `origin: {kind: "fork", session_id, ref, title, exists}`; deleting either chat leaves the other. A fork copies the conversation, not the workspace: files stay as they are now. A whole copy is refused (409) while a run is live, stopping, or resumable |
| `GET /api/sessions/{id}/export?format=md\|json\|txt` | export a chat |
| `GET /api/sessions/{id}/grants` | the chat's remembered approvals: `[{scope, context, created_at, active}]` — `active` is false for a grant given under another agent or workspace root (listed, inert). A subagent task shares the grants of the chat at the top of its task tree: an "always allow" given in either is saved there, and a task lists (and revokes) that chat's |
| `DELETE /api/sessions/{id}/grants?scope=&context=` | revoke one grant → `{removed}`; later calls in that scope ask again, a call it already approved is unaffected |
| `GET /api/sessions/{id}/ui-data/{ref}` | a UI component's full props, stored by `put_ui_data` during a run of this chat ([details](web-server.md#bulk-data)) |
| `GET` / `POST /api/schedules`, `GET` / `PATCH` / `DELETE /api/schedules/{id}`, `POST .../run` | [scheduled runs](web-server.md#scheduling): list, create, retime/pause, delete, fire now |
| `GET /api/schedules/{id}/runs` | a schedule's fire history (its run records, newest first); a fire skipped because the previous one was still going reads `status: "skipped"` |
| `POST /api/schedules/preview` | `{trigger_kind, trigger_expr}` → `{next_fires, utc_offset}`: the next three fire times were it saved now, and the server's UTC offset in seconds; 422 for an invalid trigger |
| `GET /api/workspace` · `/files` · `/file` · `/raw` | read-only file panel over the agent's [workspace](workspace.md) — the chat's directory with `?session=`, a not-yet-sent draft's with `?root=` (checked like a new chat's) |
| `GET /api/workspace/paths` | every file under the folder → `{paths, truncated}`: root-relative and sorted, the composer's `@` menu's list. The Agent's own `list_files('.', pattern='**/*')` through the panel's read-only view — git's ignore rules in a work tree, no dotfiles, nothing `denied_paths` hide, no directories — run on the host for a remote chat. `truncated` when the 20,000-entry cap (directories count toward it) left files out. `?session=` / `?root=` as for the panel |
| `POST /api/workspace/changes` | `{paths}` (up to 500, as the chat's tool calls spelled them) → `{git, truncated, files: [{path, is_dir, touched, status, old_path, added, removed, size, mtime}]}`: the paths that still exist, newest first (`touched: true`), then — when the folder is in a git work tree — the other uncommitted changes `git status` sees under it, up to 200 (`truncated` says more exist). `status` is `modified`, `added`, `untracked`, `deleted`, `renamed` or `null`; `added` / `removed` are line counts against `HEAD`. `?session=` / `?root=` as for the panel |
| `POST /api/workspace/reveal` · `/edit` | `{path}` (`.` is the root) → 204: select the entry in this machine's file manager (Finder, Explorer; on Linux, open its folder), or open a file in an editor — the app its type opens with when that is a known editor, else the plain-text editor, so the file is never run (macOS and Windows only). Only on a server with no auth guard (`/api/info`'s `desktop`, else `null` and no routes), for a request whose `Host` and `Origin` are local; the panel's read rule applies, and a remote workspace is 404 `feature_unavailable`. `?session=` / `?root=` as for the panel |
| `GET /api/workspace/diff?path=&old=` | `{path, diff, binary, truncated}`: the file against `HEAD`, unified (`old`: a rename's former path, so the move diffs as one), cut to the workspace's shell output limit. 404 `feature_unavailable` outside a git work tree or before its first commit |
| `POST /api/sessions/{id}/attachments` | multipart `file` → `{name, mime, kind, size}`: save one composer attachment in the chat's attachment folder (beside the chat history, never in the workspace), within the extension allowlist and size limit, at a collision-free `name` the message then sends back as `attachments: [{name, mime, kind}]`. `{id}` may name a chat that doesn't exist yet — a draft's, which its first send then creates the chat with — and must be 32 lowercase hex characters (422 otherwise). When the chat's agent (`?agent=` for a draft) has no workspace it takes images only (PNG, JPEG, GIF, WebP; 415 `unsupported_file_type` otherwise) — sent inline, there being no file tools; 404 `feature_unavailable` when its workspace is bound to a session the caller opened itself — shared by every chat, it can't keep one chat's attachments read-only |
| `GET /api/sessions/{id}/attachments/{name}?download=` | one attachment's bytes, by that `name`: inline for images and PDFs, a download for any file with `download=1` (as `/api/workspace/raw`). A fork or a task reads its source's — they share the folder; a deleted chat's are gone (404) |
| `GET /api/sessions/{id}/processes` | the chat's [background processes](workspace.md#background-processes), oldest first → `[{process_id, command, title, status, exit_code, cwd, started_at, ended_at, ended_by, output_chars, last_line}]`, `title` is the `description` the model gave the shell call (`""` when none), `status` one of `running` / `exited` / `killed`; `ended_by` is `exit`, `user` (the kill route below), `model`, `shutdown` (the server went down), `closed` (its workspace session did: the chat moved to another directory) or `lost` (the connection to its host dropped). A finished process leaves a record, listed across restarts (the newest 30 per chat; none for a chat whose id the client chose in another form than 32 hex digits). Listing reads no output, so the model's own reads are unaffected |
| `GET /api/sessions/{id}/processes/{process_id}/output?after=&limit=` | one page of a process's output → `{text, base, next, eof}`. Offsets count characters from the start of the process; `base` is the oldest still kept, and `text` starts there when it is past `after`. Follow a running process by asking again from `next` until `eof`. `limit` is at most 262144, the default. 404 `process_not_found` |
| `GET /api/sessions/{id}/processes/{process_id}/log` | everything kept of its output, as a text file. 404 `process_not_found` |
| `POST /api/sessions/{id}/processes/{process_id}/kill` | stop one background process → the refreshed list. Its unread output and its exit stay for the model: the next status reminder announces it killed. 404 `process_not_found` for an unknown process or no live session |
| `GET /api/workspaces` | with `create_app(workspaces=)`: the directories a new chat may pick — `{workspaces: [{id, root, name, kind, last_used_at}], default, can_add, roots}`. `kind` is `"default"` (the agent's own directory, where a new tab starts), `"general"` (the switch's catch-all) or `"added"`; the first two have no `id` and stay listed. `can_add` says whether others may be added; `roots` lists the allowed trees when only some are |
| `POST /api/workspaces` · `DELETE /api/workspaces/{id}` | `{path}` → the entry: add an existing, allowed directory (symlinks resolved) · drop one from the list (its files and chats stay; a new chat there lists it again) |
| `PATCH /api/workspaces` | `{root, name}` → the entry: a listed directory's display name (the folder is not touched; `""` restores its own name). The general directory keeps the page's name for it |
| `GET /api/workspaces/dirs?path=` | completions for a directory being typed: subdirectory paths as typed (`~` kept), within what may be added. With `&host=` — the front of a remote location's key, `ssh://box` — the folders are that host's; a host that isn't offered or can't be reached suggests nothing |
| `POST /api/sessions/associate` | `{session_ids, root}` → `{associated, root}`: bind chats from before chats had a directory (`workspace_root: null`) to one, for good — one at a time is `PATCH /api/sessions/{id}` with `workspace_root`. A chat whose run is live in another directory (a schedule fire in the agent's own) is refused with `run_active` until it ends |
| `GET /api/skills?agent=` | the skills a run sees → `[{name, description}]`, in the prompt index's order: the agent built as the run builds it (a [workspace switch](web-ui.md#workspaces)'s project hooks included), then each `Skills` plugin's catalog with its filter and precedence. `?session=` for an existing chat, `?root=` for a draft about to open there |
| `GET` / `PUT /api/memory?agent=` | read / replace the [Memory notes](memory.md#how-memories-get-written) (`{content, used, budget}`) |
| `GET /api/config` · `POST /api/config/models` · `PUT`/`DELETE /api/config/models/{id}` · `PUT /api/config/roles` · `PUT /api/config/search` · `PUT`/`GET /api/config/skills` · `PUT /api/config/assistant` · `PUT /api/config/voice` · `POST /api/config/test` · `POST /api/config/mcp` · `PUT`/`DELETE /api/config/mcp/{name}` · `PUT /api/config/mcp/{name}/enabled` · `GET /api/config/mcp/{name}/secrets` · `GET`/`PUT /api/config/project/mcp` · `POST /api/config/mcp/test` | **UI-internal**, serving the CLI's built-in settings UI and not part of the [stable API](stability.md): the CLI's [model configuration](web-ui.md#models-and-switching), [skill directories](web-ui.md#skills), [MCP servers](web-ui.md#mcp-servers) and [assistant settings](web-ui.md#the-assistant) — present only when `/api/info` reports `features.settings`. Keys are write-only (`null` keeps, `""` clears; reads carry `{set, hint}`); MCP `env` and `headers` values read back masked, and a mask sent back unchanged keeps the stored value; `…/secrets` returns one server's values unmasked (Settings' Show secrets), to a local origin or with the token. Writes validate the whole document, persist it, and hot-swap the served agents. `GET /api/config/skills` reports per-directory discovery |

`POST /api/chat` and `/api/chat/stream` take
`{message, session_id?, agent?, attachments?, context?}`, and `POST /api/schedules`
takes a `context` too. `context` is a JSON object of at most 64 KB — the client
context `create_app(run_deps=...)` receives as `trigger.client_context` (see
[Run deps](web-server.md#run-deps)). A message that joins a live run
doesn't change that run's context.

On a server with `create_app(workspaces=)` (`features.workspace_switch`), a
new chat's request may carry `workspace` — its directory, an absolute path
(`~` expands), checked against the server's `roots` (else the agent's own) —
and `workspace_mode` (`"readonly"`, `"coding"` or `"trusted"`, one of
`/api/info`'s `workspace_modes.modes`). The directory is fixed for the chat's
life and checked again on every run: one this server no longer allows is
refused (409 `workspace_not_allowed`), never swapped. An existing chat keeps
its mode too, which `PATCH /api/sessions/{id}` changes at once — for a run in
flight and for the chat's subagent tasks as well.
Sessions report `workspace_root` (`null`: from before chats had one). Sessions report
`workspace_mode` as stored, with `workspace_mode_rev`, which each switch
bumps: of two reports of a chat's mode (a list, a detail, a `PATCH` answer,
an event), the higher revision is the newer, in whatever order they arrive.
A mode the server no longer offers runs as the widest offered one below it.

On the CLI's server (`features.chat_model`), a new chat's request may carry
`model` — the `id` of a configured model profile — and
`PATCH /api/sessions/{id}` changes it from the chat's next run (`null`: follow
the default again). An unknown id is refused (400 `invalid_request`). Sessions
report `model`, `null` while the chat follows the default; a stored id whose
profile was deleted runs on the default.

`/api/chat/stream` also takes `busy`: what a message for a chat with a live
run does. `"inject"` (the default) steers it into that run; `"reject"` answers
409 `run_active` instead and never injects — for a message that must start a
run of its own, such as a follow-up queued until the current run ends. A chat
already busy on arrival is refused before anything runs or is written; a run
that starts meanwhile is still refused, with no injection and no new run.

### Lifecycle events

`GET /api/events` uses GET + `EventSource` to publish:

| Lifecycle event | Payload |
| --- | --- |
| `run_started` | `{session_id, run_id, agent, source}` |
| `run_finished` | `{session_id, run_id, status, error, source}` |
| `run_status` | `{session_id, run_id, status}` — a live run started (`blocked_on_approval`) or stopped (`running`) waiting on an approval, or started or stopped waiting on an `ask_human` question (`status` stays `running`); `GET /api/runs` carries the request and `waits_on` |
| `session_created` | `{session_id, agent, title}` |
| `session_retitled` | `{session_id, title}` |
| `session_appended` | `{session_id}` — entries landed in the chat without a run (a subagent report held after a stop); refetch the session |
| `session_workspace_changed` | `{session_id, workspace_mode, workspace_mode_rev, workspace_root}` |
| `processes_changed` | `{session_id, running}` — a background process of the chat started or ended; refetch its list |
| `config_changed` | `{configured, model, profile_id, name}` — refetch `/api/config` |

It does not replay history. On every connection or reconnection, fetch current state from
`/api/sessions` and `/api/runs` before processing new events. The server closes
subscribers that fall behind; recover them with the same snapshot-first flow.
To find Runs that finished while disconnected, query `/api/runs/history` with
`since`.

### Other behavior

- Starting a chat stream for a Session with a live Run attaches to that Run
  instead of starting a second one or failing.
- Workspace routes always access the workspace in read-only mode, regardless
  of the Agent's mode, and preserve its `denied_paths`. They hide regenerable
  files such as `__pycache__`, `*.pyc`, `venv`, and `node_modules`. The
  folder listing (`/api/workspace/files`) shows dotfiles, except `.git` and
  any the read rules don't allow (`.env`, credential stores).

### Blocking turns

`POST /api/chat` runs its turn as the same supervised Run a stream starts,
and answers once that Run ends. So the Run holds the chat for the whole
turn: another `/api/chat` for it gets 409 `run_active`, while a stream's
message, a subagent's report, or a scheduled fire for the chat is injected
into it — `output` is then the Run's last answer.
`/api/chat/cancel`, deleting the chat, or shutdown stops it (409
`run_cancelled`); a failure answers 500 `run_failed`; it counts toward the
concurrent-run cap (429). A client that disconnects mid-turn doesn't stop
it: the Run finishes into the chat, as a detached stream's does.

No client sees a blocking turn's approval requests, so its gated Tool calls
don't wait for one: the Agent's `approval_handler` decides, else the call is
denied — as for [`Runner.run`](tools.md#tool-approval).

## Chat SSE streams

`POST /api/chat/stream` and `/api/chat/reconnect` return a `text/event-stream`
of `event:` / `data:` pairs: the Runner's
[typed events](streaming.md#event-catalog), with JSON-encoded data.

`?` marks a field that may be absent.

| SSE event | Payload |
| --- | --- |
| `session` | `{session_id, run_id}` — first frame of every stream: the chat, and the run it follows (the id its lifecycle events carry) |
| `snapshot` | `{session_id, status, entries}` — re-attach prologue: the completed turns so far |
| `text_delta` | `{delta}` |
| `reasoning_delta` | `{delta}` |
| `output_discarded` | `{}` — clear the current turn's rendered deltas |
| `message_completed` | `{message, usage}` — one assistant turn, assembled; `usage` is that model call's, with `last_input_tokens` its prompt size and, when known, `context_window` the window it was budgeted against (run records keep the last one) |
| `user_injected` | `{content, turn}` |
| `tool_call` | `{id, name, arguments}` — `arguments` is the raw JSON string |
| `tool_result` | `{id, name, result, is_error, images?, arguments?}` — `images` lists `{index, mime_type}` stubs served by `GET /api/sessions/{id}/tool-images/{call_id}/{index}`. An error carries the call's `arguments`: a call rejected before it started (denied by policy, unknown tool, malformed arguments, …) sends no `tool_call` first |
| `todo` | `{call_id, name, todos}` — a structured todo update, in place of that call's `tool_result` |
| `approval_required` | `{id, name, arguments, reason, scopes, granted, expires_at}` → answer via `POST /api/chat/approve`. `reason` says why the call asks (empty when nothing stands out beyond "not pre-approved"); `scopes` is what "always allow" would cover — every entry, remembered together (`[]` = don't offer it); `granted` is true when remembered approvals already answered it — show a note, not a card; `expires_at` (epoch seconds, `null` = never) is when an unanswered request stops waiting live: the run suspends |
| `approval_resolved` | `{id, approved, cause}` — the request `id` was settled: `cause` is `user` (an answer from any tab), `cancel` (the run was stopped) or `policy` (the chat's permission mode was switched while it waited, and the new mode allows or refuses the call). Sent to every attached client and replayed with the turn, so a card never stays open once decided |
| `suspended` | `{id}` — the run stopped at call `id` to wait for its answer: an approval card nobody answered by `expires_at`, an `ask_human` question nobody answered within `suspend_after`, or a tool that put its answer off. The stream ends, with no `done`. The card stays open; answering it (`POST /api/chat/approve` or `POST /api/chat/answer`, any time, even after a restart) resumes the run. Meanwhile the chat's `GET /api/sessions/{id}` carries it as `waiting` (`kind` `approval`, `question`, or `call` for another tool's wait, which a message to the chat resumes) and `GET /api/runs` lists the run as `waiting` |
| `handoff` | `{from, to}` |
| `turn_started` | `{turn, agent}` |
| `retrying` | `{attempt, max_attempts, delay, message}` — the model call failed transiently; attempt `attempt + 1` follows in `delay` seconds |
| `context_compacting` | `{active}` — the context policy began (`true`) or ended (`false`) slow [compaction](context.md) work; always a pair, and `false` precedes any `context_compacted` |
| `context_compacted` | `{session_id, reason, reactive, summary, tokens_before, tokens_after, detail}` — a [compaction notice](context.md) |
| `error` | `{type, message, code, status_code?, retryable?, hint?}` — tool-scoped, or terminal when the stream then ends |
| `refs` | `{refs}` — the `ref`s of the user messages the run just finished holds, in order; precedes `done`. Absent without a checkpointer, or when the run holds a user entry the stream never showed (a structured-output repair prompt): the next load of the chat names them |
| `timing` | `{finished_at, duration}` — the run just finished, as its segment recorded it (epoch seconds; seconds), so a live answer reads as on reload; precedes `done`. Absent when the segment went untimed |
| `mcp_unavailable` | `{servers}` — `[{name, error}]`: Settings → MCP servers this run could not connect to, as it starts; it goes on without their tools. Live only, not replayed on reload |
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
scheduling, Workspace, Memory, and related endpoints. Methods that decode JSON
reject with an `Error` carrying `status`, `code`, and `hint`. The streams
(`streamChat`, `reconnect`) and the bodiless actions (`approve`, `answer`,
`cancel`, `deleteSession`, `deleteAllSessions`) resolve to the raw `Response`
instead; turn a failed one into the same `Error` with `apiError(response)`,
which is exported for your own `fetch` calls too. `readSSE(response)` is an
async generator over a stream's `{event, data}` pairs:

```js
import { api, readSSE } from "./api.js";

const res = await api.streamChat({ message: "hello" });
for await (const { event, data } of readSSE(res)) {
  if (event === "text_delta") render(data.delta);
}
```

Copy it into your own front end, or read it as the reference implementation
for any language — it is intentionally small. Like the page's other
JavaScript modules it is not part of the [stable surface](stability.md).

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
  `create_app(token=...)` adds token authentication, and `serve()` never
  exposes the API anonymously off loopback ([Authentication](#authentication)). User identities, permissions, quotas,
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
