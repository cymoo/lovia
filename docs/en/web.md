# Web UI & server

An agent isn't real to anyone until they can talk to it. The optional web
layer is a small FastAPI app — chat UI, SSE streaming, sessions with
titles, approvals, schedules, a memory editor, a read-only file panel — that
serves any lovia agent, and whose [HTTP API](http-api.md) stands alone when
you'd rather bring your own front-end.

```bash
pip install "lovia[web]"
```

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

Or skip code entirely:

```bash
python -m lovia.web
```

## `serve()` and `create_app()`

`serve(agent_or_agents, *, host="127.0.0.1", port=8000, ...)` builds the
app and runs uvicorn (extra kwargs — `log_level`, `ssl_certfile`, `workers`
— pass through). `create_app(...)` returns the ASGI app for your own
process manager. The options that matter:

| Option | Default | Effect |
| --- | --- | --- |
| `agent_or_agents` | required | one agent, or a `{name: agent}` dict to serve several |
| `db_path` / `store` / `session` | `<agent>.db` in cwd | where transcripts + metadata live ([`ChatStore`](http-api.md#chatstore)) |
| `max_turns` / `budget` / `retry` / `context_policy` / `tracer` | — | run settings applied to every served run |
| `generate_titles` / `title_model` | `True` / serving agent's model | background LLM chat titles (first user line shows until one lands; a manual rename always wins) |
| `approval_timeout` | `None` | auto-deny pending approvals after N seconds |
| `max_background_runs` | `8` (`create_app`) | concurrent supervised runs; excess starts get HTTP 429 |
| `ui` | `True` | `False` = API only (no `GET /` or `/static`) |
| `cors_origins` | `None` | unset = no CORS headers (cross-origin browsers refused) |
| `title` / `empty_title` / `empty_description` | `"lovia"` / `"Wake up, Neo."` / … | branding |

## The zero-config CLI

`python -m lovia.web` builds a default agent and serves it. The
composition, exactly: model from the environment; `Skills("./skills")`
when that directory exists; a `Todo()` checklist; `Scheduling` (the agent
can propose its own future runs — approval-gated); `Memory` under
`./.lovia/memory` with background curation; tools `now` + `http_fetch`
(plus `web_search` when the `ddg` backend is installed); a **trusted
workspace on the current directory**; today's date as an instruction
fragment; instructions from `AGENTS.md` when present.

Every option reads flag → env var → default, and a `./.env` loads
automatically when `python-dotenv` is installed (or pass `--env-file`).
Model credentials use the provider's own variables
([Providers](providers.md)).

| Flag | Env | Default |
| --- | --- | --- |
| `--host` / `--port` | `LOVIA_HOST` / `LOVIA_PORT` | `127.0.0.1` / `8000` |
| `--db` | `LOVIA_DB` | `<agent>.db` in cwd |
| `--model` | `LOVIA_MODEL` → `OPENAI_DEFAULT_MODEL` → `ANTHROPIC_DEFAULT_MODEL` | required |
| `--skills-dir` (repeatable) | `LOVIA_SKILLS_DIR` | `./skills` if present |
| `--memory-dir` / `--no-memory` | `LOVIA_MEMORY_DIR` | `./.lovia/memory` (on) |
| `--workspace` / `--workspace-mode` / `--no-workspace` | `LOVIA_WORKSPACE` / `LOVIA_WORKSPACE_MODE` | `.` / `trusted` (on) |
| `--instructions` / `--instructions-file` | `LOVIA_INSTRUCTIONS_FILE` | `AGENTS.md`, else generic |
| `--app MODULE:ATTR` | `LOVIA_APP` | build the default agent |
| `--title` / `--log-level` | `LOVIA_TITLE` / `LOVIA_LOG_LEVEL` | `lovia` / `info` |
| `--max-retries` | `LOVIA_MAX_RETRIES` | agent posture (3 retries); `0` disables |
| `--provider-timeout` | `LOVIA_PROVIDER_TIMEOUT` | `60`s |
| `--max-tokens` / `--context-window` | `LOVIA_MAX_TOKENS` / `LOVIA_CONTEXT_WINDOW` | provider default / ask the provider |
| `--max-turns` | `LOVIA_MAX_TURNS` | `50` |
| `--trust-env` | `LOVIA_PROVIDER_TRUST_ENV` | off (on → honor `HTTP(S)_PROXY`) |
| `--env-file` (repeatable) / `--version` | — | `./.env` if present / print version |

`--app mymodule:assistant` serves your own agent (the default-agent flags
are then ignored, with a warning). `--provider-timeout` and `--trust-env`
act on the providers themselves, so they apply to `--app` agents too;
`--max-retries` / `--max-turns` apply to every served run;
`--max-tokens` / `--context-window` configure the default agent only.

For TLS to intranet model hosts, the `web` extra bundles `truststore`, so
the OS certificate store is trusted automatically —
`LOVIA_HTTP_CA_BUNDLE` / `LOVIA_HTTP_INSECURE` remain the
[manual overrides](providers.md#networking-timeouts-proxies-tls).

## Runs outlive the browser

Served runs are **supervised**: the run is a server-owned task, and an SSE
connection is just a subscriber. Close the laptop mid-run and the run keeps
going; reopening the chat re-attaches — the client receives an
authoritative snapshot of completed turns, a replay of the current turn's
in-flight events (including a still-pending approval), then the live tail.

The lifecycle around that:

- **Stop** (`POST /api/chat/cancel`, the UI's stop button) cancels the run
  and finalizes its completed turns into the session as a
  [caller-decided partial](sessions-and-checkpoints.md#the-contract)
  (dangling tool calls dropped, checkpoint cleared) — the conversation
  keeps what the user saw, and nothing double-counts.
- **Server shutdown** cancels supervised runs cooperatively but *keeps
  their checkpoints*, so a redeploy resumes interrupted runs on reconnect
  (`POST /api/chat/reconnect`); background [memory curation](memory.md)
  is drained with a bounded wait.
- **Capacity**: at `max_background_runs`, new starts get 429 and the
  scheduler defers its fires.

## Scheduling

The server runs a small scheduler over a durable `schedules` table
(created via the API or by the agent itself):

- **Triggers**: `at` (one-shot — ISO-8601 or epoch), `every` (interval
  seconds), `cron` (via `croniter`, bundled with `lovia[web]`).
- **The `schedule_run` tool** (default agent, or `Scheduling(store)` on
  your own) lets the *model* propose follow-ups — "remind me Friday" —
  gated by [approval](human-in-the-loop.md), so nothing is scheduled
  without a click. `continue_session=True` lands results in the same chat;
  otherwise each fire opens a fresh session.
- **Delivery is at-most-once and coalesced**: a fire is skipped (not
  queued) while the previous one is still running, and paused schedules
  stay paused (`PATCH` with `active`, `POST .../run` to fire manually).

## Sharp edges

- **There is no authentication.** The server trusts every request — bind
  to loopback (the default), or put your own auth proxy in front before
  exposing it. The CLI warns loudly when you combine a non-loopback host
  with a writable workspace, because that combination is *remote code
  execution as your user*.
- **The default workspace mode is `trusted` on your cwd** — right for a
  personal assistant in a project directory, wrong for anything shared.
  `--workspace-mode readonly` or `--no-workspace` first, loosen later.
- **Supervised state is per-process.** Live runs, approvals, and SSE hubs
  live in memory: run one process (`workers=1`); the SQLite stores use
  `wal` so *data* survives, but a multi-worker deployment needs sticky
  routing you probably don't want to build.
- **`/api/chat` (blocking) is not supervised** — no reconnect, no attach.
  UIs should use `/api/chat/stream`; the blocking route is for scripts.

## See also

- [HTTP API](http-api.md) — every endpoint, the SSE wire format, BYO front-end
- [Memory](memory.md#how-memories-get-written) — the sidebar editor's backend
- Example: [`26_web_serve.py`](../../examples/26_web_serve.py)
