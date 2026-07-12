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
| `max_background_runs` | `8` | Concurrent supervised Runs; excess starts return 429 |
| `ui` | `True` | Set `False` for API-only serving |
| `cors_origins` | `None` | Allowed browser origins; unset sends no CORS headers |
| `title` / `empty_title` / `empty_description` | lovia defaults | UI copy and branding |

For endpoint contracts and the `ChatStore` interface, see
[HTTP API](http-api.md).

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

## Security checklist

- Keep `host="127.0.0.1"` unless an authenticated reverse proxy protects the app.
- Restrict or disable writable Workspace access for untrusted users.
- Configure `approval_timeout` so abandoned dialogs do not occupy capacity.
- Use one worker and back up the SQLite database.
- Add request-level authentication, authorization, and rate limiting before network exposure.

See the complete [Deployment](deployment.md) guide before production use.

## See also

- [Web UI](web-ui.md) — built-in browser experience and CLI
- [HTTP API](http-api.md) — endpoints, SSE wire format, and `ChatStore`
- [Tools: approval](tools.md#tool-approval) — server-side approval flow
