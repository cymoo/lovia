# Web server

The Web package is built on FastAPI. `create_app()` builds the ASGI
application and holds every serving option; `serve()` runs it under uvicorn.

```bash
pip install "lovia[web]"
```

```python
from lovia import Agent
from lovia.web import create_app, serve

agent = Agent(name="assistant", model="<model>")
serve(create_app(agent, db_path="lovia.db"), host="127.0.0.1", port=8000)
```

## `create_app()` and `serve()`

`serve(target, *, host="127.0.0.1", port=8000, **uvicorn_kwargs)` runs an app
from `create_app()`, or builds one with the defaults when given an Agent (or a
`{name: agent}` mapping) directly — `serve(agent)` is the quickest start.
Server options such as `log_level`, `ssl_certfile`, and `workers` pass through
to uvicorn. To run the app under another ASGI server, skip `serve()`.

`create_app(agent_or_agents, ...)` options:

| Option | Default | Description |
| --- | --- | --- |
| `agent_or_agents` | required | One Agent or a `{name: agent}` mapping |
| `db_path` / `store` / `session` | `./.lovia/<agent>.db` | Transcript and chat metadata storage |
| `max_turns` / `budget` / `retry` / `context_policy` | — | Settings applied to every served Run |
| `tracer` | `None` | Span recorder for served Runs |
| `generate_titles` / `title_model` | `True` / Agent model | Generate conversation titles in the background |
| `followups` / `followup_model` | `False` / Agent model | Suggest follow-up questions after a reply (see below) |
| `approval_timeout` | `None` | Auto-deny unresolved approvals after N seconds |
| `max_background_runs` | `8` | Concurrent supervised Runs; excess starts return 429 |
| `ui` | `True` | Set `False` for API-only serving |
| `cors_origins` | `None` | Allowed browser origins; unset sends no CORS headers |
| `token` / `auth` | `None` | Bearer-token guard for business API routes, or your own FastAPI dependency (see below) |
| `title` / `empty_title` / `empty_description` | lovia defaults | UI copy and branding |
| `empty_examples` | `None` | Clickable starter prompts on the blank chat state (clicking fills the composer) |

Transcripts, chat metadata and run checkpoints share one SQLite file, opened in
**WAL mode** so the UI's reads never wait behind a checkpoint write. An existing
database migrates on first open. Its `<name>.db-wal` and `<name>.db-shm`
companions exist only while a connection is open, and the last one to close
folds the WAL back into the database — so copy all three from a running server,
but a stopped one still leaves a single file. On a filesystem without shared
memory (some network mounts) SQLite declines WAL and logs a warning, keeping the
old journal mode; pass `store=ChatStore.sqlite(path, wal=False)` to opt out up
front.

For endpoint contracts and the `ChatStore` interface, see
[HTTP API](http-api.md).

## Follow-up suggestions

After a Run produces an answer, the built-in suggester makes a separate small
model call. The main Transcript never sees that request, and the call does not
delay the original answer.

```python
create_app(agent, followups=True, followup_model="<small-model>")
```

Off by default: unlike a title, this costs one model call per *run*, so the
serving layer doesn't presume it. `followup_model` points that call at a
cheaper model than the Agent's own. The bundled `lovia web` CLI opts in on its
own behalf — disable it with `--no-followups` or `LOVIA_FOLLOWUPS=0`, and point
it at a small model with `LOVIA_FOLLOWUP_MODEL`.

Pass a callable to replace the built-in suggester entirely — back it with a
curated FAQ, a vector store, or the same generator under a different prompt:

```python
from lovia.web import FollowupRequest, create_app, generate_followups

async def pricing_only(request: FollowupRequest) -> list[str]:
    return await generate_followups(
        request, model="<model>", instructions="Ask only about pricing."
    )

create_app(agent, followups=pricing_only)
```

A suggester receives the session's conversation (`session_id`, `agent`, and the
chat-shaped `messages`) and returns any number of strings. Returning `[]` shows
nothing, which is also what a raising suggester degrades to — the chips are
never load-bearing.

## Authentication

Loopback binds need no credentials. `serve()` is safe by default beyond that:
an Agent served on a non-loopback host gets a generated token, printed once
together with a ready `/?token=...` UI link, and an app built with neither
`token` nor `auth` is refused there. The business API is never exposed
anonymously on a non-loopback address.

```python
serve(create_app(agent, token="s3cret"), host="0.0.0.0")   # fixed token
serve(agent, host="0.0.0.0")                                # generated + printed
```

The token guards the business routes registered by `build_api_router`.
`/healthz`, `/api/docs`, `/api/openapi.json`, the UI shell, and static assets
remain public. Clients can supply the token in two ways:

- **Plain API requests and chat SSE** send
  `Authorization: Bearer <token>`. Chat streams use `fetch`, so headers work.
- **The bundled UI** stores the token in a cookie. `/api/events` uses
  `EventSource`, which cannot set custom headers, so it authenticates with the
  cookie; `<img>` previews and download links use it too. The UI reads the
  token from a `/?token=...` link or asks for it after a 401.

For sessions, OAuth, or per-user identity, replace the built-in check with any
FastAPI dependency — it guards the same routes:

```python
async def my_auth(request: Request) -> None:
    if not valid(request):
        raise HTTPException(status_code=401)

serve(create_app(agent, auth=my_auth), host="0.0.0.0")
```

It guards exactly what the token would, which has three consequences:

- **The bundled UI needs a cookie.** `EventSource`, `<img>` previews, and
  download links cannot send custom headers, so a dependency that reads only
  `Authorization` locks them out. Accept your session cookie as well.
- **Routes you add are yours to guard.** Include them with
  `dependencies=[Depends(my_auth)]`. The UI shell and `/api/docs` stay
  public; to gate the whole site, use middleware or your reverse proxy.
- **A front end on another origin** that authenticates with cookies needs
  credentialed CORS, which `cors_origins` does not enable (see below).

For that cross-origin case, leave `cors_origins` unset and add the middleware
yourself:

```python
from fastapi.middleware.cors import CORSMiddleware

app = create_app(agent, auth=my_auth)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chat.example.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

If the two origins are different *sites* (not just subdomains of one), the
cookie must be `SameSite=None`, so defend against CSRF yourself: several
endpoints (cancel, run a schedule now, upload) are body-less or multipart
POSTs — "simple" requests that skip the CORS preflight. The token cookie is
`SameSite=Strict` and unaffected.

`create_app()` alone enables no authentication; under another ASGI server,
pass `token` or `auth` yourself. `serve()` only judges apps built by
`create_app()` — an app of your own that mounts `build_api_router` keeps its
own middleware and is run as is.

## Supervised Run lifecycle

Streaming Runs are server-owned tasks. An SSE subscriber may disconnect and
reattach without cancelling work.

- **User cancellation** finalizes completed turns into the Session, removes
  dangling Tool calls, and clears the checkpoint.
- **Server shutdown** cancels Runs cooperatively but keeps checkpoints, so a
  client can reconnect and resume after deployment.
- **Capacity** is bounded by `max_background_runs`; new starts receive HTTP 429
  while full.
- **Blocking `/api/chat`** is not supervised. Front ends should use
  `/api/chat/stream`.

Live Runs, approvals, and SSE hubs are process-local. Run one worker. SQLite
data uses WAL and survives restarts, but it does not make in-memory supervision
safe across multiple workers.

## Scheduling

The Web package stores durable schedules and supports three trigger forms:

| Trigger | Value |
| --- | --- |
| `at` | One ISO-8601 timestamp or epoch time |
| `every` | Interval in seconds |
| `cron` | Cron expression, matched against the server's local time; `croniter` ships with `lovia[web]` |

`Scheduling(store)` contributes the approval-gated `schedule_run` Tool. The
model can propose a future run, but it is not created until a user approves the
Tool call. With `continue_session=True`, a fire joins the same chat and injects
its instruction into an active Run. With `continue_session=False`, each fire
starts a new Session and is skipped rather than queued while the previous fire
is still running. After downtime, multiple missed fire times coalesce into one.

A repeating schedule can carry a stop condition (`until`, natural language):
each fire is then told to evaluate it after doing the task and cancel the
schedule once it is met — "check the log every minute until it says ready".
Deterministic safety nets (`max_fires`, `expires_at`) deactivate the schedule
even if the condition is never met; the tool requires one when `until` is set.
The plugin also contributes `list_schedules` and `cancel_schedule`, which need
no approval: cancelling deactivates the schedule without deleting its record,
and self-cancel must work inside a clientless scheduled run, where an
approval request would be auto-denied.

## Security checklist

- Keep `host="127.0.0.1"` for personal use; non-loopback binds are
  token-guarded automatically, but the token then protects everything —
  treat it like a password.
- Restrict or disable writable Workspace access for untrusted users: anyone
  holding the token can make the agent edit files or run shell commands.
- Configure `approval_timeout` so abandoned dialogs do not occupy capacity.
- Use one worker and back up the SQLite database.
- For real multi-user exposure add TLS, per-user auth (`auth=`), and rate
  limiting — a shared token is single-user security.

See the complete [Deployment](deployment.md) guide before production use.

## See also

- [Web UI](web-ui.md) — built-in browser experience and CLI
- [HTTP API](http-api.md) — endpoints, SSE wire format, and `ChatStore`
- [Tools: approval](tools.md#tool-approval) — server-side approval flow
