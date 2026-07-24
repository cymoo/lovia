# Web server

The optional Web package provides a small FastAPI server for one or more
Agents. Use `serve()` for a standalone process or `create_app()` when your
deployment already owns the ASGI lifecycle.

```bash
pip install "lovia[web]"
```

```python
from lovia import Agent
from lovia.web import serve

agent = Agent(name="assistant", model="<model>")
serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

## `serve()` and `create_app()`

`serve(agent_or_agents, *, host="127.0.0.1", port=8000, ...)` creates the
application and runs uvicorn. Extra server options such as `log_level`,
`ssl_certfile`, and `workers` pass through. `create_app(...)` returns the ASGI
application without starting a process.

| Option | Default | Description |
| --- | --- | --- |
| `agent_or_agents` | required | One Agent or a `{name: agent}` mapping |
| `db_path` / `store` / `session` | `./.lovia/<agent>.db` | Transcript and chat metadata storage |
| `max_turns` / `budget` / `retry` / `context_policy` | — | Settings applied to every served Run |
| `tracer` | `None` | Span recorder for served Runs |
| `generate_titles` / `title_model` | `True` / Agent model | Generate conversation titles in the background |
| `approval_timeout` | `None` | Auto-deny unresolved approvals after N seconds |
| `max_background_runs` (`create_app()` only) | `8` | Concurrent supervised Runs; excess starts return 429 |
| `ui` | `True` | Set `False` for API-only serving |
| `cors_origins` | `None` | Allowed browser origins; unset sends no CORS headers |
| `token` / `auth` | `None` | Bearer-token guard for business API routes, or your own FastAPI dependency (see below) |
| `title` / `empty_title` / `empty_description` | lovia defaults | UI copy and branding |
| `empty_examples` | `None` | Clickable starter prompts on the blank chat state (clicking fills the composer) |

`serve()` always uses `max_background_runs=8`. To change it, build the app with
`create_app()` and run it with an ASGI server.

For endpoint contracts and the `ChatStore` interface, see
[HTTP API](http-api.md).

## Authentication

Loopback binds need no credentials. `serve()` is safe by default beyond that:
binding a non-loopback host with neither `token` nor `auth` generates a token
and prints it once, together with a ready `/?token=...` UI link. This prevents
the business API from being exposed anonymously on a non-loopback address.

```python
serve(agent, host="0.0.0.0", token="s3cret")        # fixed token
serve(agent, host="0.0.0.0")                        # generated + printed
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

serve(agent, host="0.0.0.0", auth=my_auth)
```

`create_app()` accepts the same two parameters but enables no authentication
by default. Pass either `token` or `auth` explicitly when you own the app.

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
| `cron` | Cron expression; `croniter` ships with `lovia[web]` |

`Scheduling(store)` contributes the approval-gated `schedule_run` Tool. The
model can propose a future run, but it is not created until a user approves the
Tool call. `continue_session=True` appends results to the same chat; otherwise
each fire starts a new Session. Delivery is at-most-once and coalesced: a fire
is skipped while the previous one is still running.

A repeating schedule can carry a stop condition (`until`, natural language):
each fire is then told to evaluate it after doing the task and cancel the
schedule once it is met — "check the log every minute until it says ready".
Deterministic safety nets (`max_fires`, `expires_at`) deactivate the schedule
even if the condition is never met; the tool requires one when `until` is set.
The plugin also contributes `list_schedules` and `cancel_schedule`, which need
no approval: cancelling only deactivates (resume or delete from the panel),
and the self-cancel must work inside a clientless scheduled run, where an
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
