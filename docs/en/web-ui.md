# Web UI

The optional browser UI turns any lovia Agent into a local chat application.
It includes streaming text and Tool activity, conversation history, titles,
approvals, schedules, a memory editor, a context-usage meter, and a read-only
Workspace file panel.
All browser assets are bundled, so the page works without a CDN or external
font requests.

## Start in one command

```bash
pip install "lovia[web]"
lovia web
```

Open `http://127.0.0.1:8000`. On first launch the CLI asks for missing model
configuration, verifies it, and can save it to `./.env`.

The default Agent includes `Todo`, optional Skills from `./skills`, Memory in
`./.lovia/memory`, time and HTTP Tools, optional DuckDuckGo search, scheduling,
and a coding-mode Workspace rooted at the current directory. If `AGENTS.md`
exists, its content becomes the Agent's instructions.

!!! danger "Local by default; token-guarded beyond that"

    The default `127.0.0.1` binding needs no credentials. Binding any other
    host requires an API token — `--token` / `LOVIA_WEB_TOKEN`, or one is
    generated and printed with a ready `/?token=...` link (the UI stores it
    and asks for it on 401). The token then protects file edits and shell,
    so treat it like a password, and prefer `--readonly` off-loopback. For
    real multi-user exposure see [Deployment](deployment.md).

## Serve your own Agent

Create `app.py`:

```python
from lovia import Agent

assistant = Agent(
    name="assistant",
    instructions="Answer clearly and use tools when they improve accuracy.",
    model="<model>",
)
```

Then point the CLI at the object:

```bash
lovia web --app app:assistant
```

`--app MODULE:ATTR` accepts one Agent or a `{name: agent}` mapping. For Python
deployment and ASGI integration, see [Web server](web-server.md).

## Useful CLI options

Every option resolves in this order: command-line flag, environment variable,
`./.env` (or `--env-file`), then default.

| Flag | Environment | Default |
| --- | --- | --- |
| `--host` / `--port` | `LOVIA_HOST` / `LOVIA_PORT` | `127.0.0.1` / `8000` |
| `--token` | `LOVIA_WEB_TOKEN` | None on loopback; generated + printed otherwise |
| `--db` | `LOVIA_DB` | `./.lovia/<agent>.db` |
| `--model` | `LOVIA_MODEL` | Asked on first run |
| `--app MODULE:ATTR` | `LOVIA_APP` | Build the default Agent |
| `--skills-dir` | `LOVIA_SKILLS_DIR` | `./skills` when present |
| `--memory-dir` / `--no-memory` | `LOVIA_MEMORY_DIR` | `./.lovia/memory` |
| `--workspace` / `--readonly` / `--no-workspace` | `LOVIA_WORKSPACE` | `.` in coding mode |
| `--instructions-file` | `LOVIA_INSTRUCTIONS_FILE` | `AGENTS.md` when present |
| `--max-retries` / `--max-turns` | `LOVIA_MAX_RETRIES` / `LOVIA_MAX_TURNS` | `4` / `50` |
| `--env-file` | — | `./.env` when present |

Run `lovia web --help` for the full list, including TLS, Provider timeout,
context-window, and proxy options.

## What happens when the browser disconnects

The server owns the Run; an SSE connection is only a subscriber. Closing or
refreshing the browser does not stop work. Reopening the conversation receives
a snapshot of completed turns, a replay of the current turn, then the live
tail. The Stop button explicitly cancels the Run and keeps completed turns in
the Session.

Running sessions show a pulsing dot in the sidebar and can be stopped from
there directly. The UI polls for background activity: when a run you're not
watching finishes, a toast appears — and if the tab is hidden, the browser
tab's title gains an unseen-count badge. Scheduled runs record the outcome of
their last fire (shown as ✓ / ✕ in the schedules dialog, with the error
message a hover away).

## See also

- [Web server](web-server.md) — Python API, lifecycle, and scheduling
- [HTTP API](http-api.md) — build a different front end
- [Tools: approval](tools.md#tool-approval) — how approval dialogs resolve
- Example: [`26_web_serve.py`](../../examples/26_web_serve.py)
