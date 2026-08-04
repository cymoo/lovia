# Web UI

The bundled Web UI is a local chat application for using a lovia Agent or
validating one before building a custom front end.

## Start in one command

```bash
pip install "lovia[web]"
lovia web
```

Open `http://127.0.0.1:8000` and configure one model on first launch. Later,
`lovia web --check` validates the configuration and probes the endpoint without
starting the server.

Model configuration has two scopes. A project file replaces the user-level
configuration as a whole; the files are not merged field by field:

| Path | Scope |
| --- | --- |
| `~/.lovia/config.json` | Default for the current user |
| `./.lovia/config.json` | Complete override for the current project |

The CLI adds `.lovia/` to `.gitignore` to protect keys and chat data. On
platforms with Unix permissions, the configuration is owner-readable and
writable only.

The default Agent includes Todo, Memory, time and HTTP Tools, web search,
scheduling, and a coding-mode Workspace rooted at the current directory. It
also discovers:

- `AGENTS.md` as Agent instructions;
- `./.agents/skills` as the Skills directory;
- `./.lovia/memory` as the Memory directory.

The legacy `./skills` directory is no longer discovered automatically. Move it
to `./.agents/skills`, or pass `--skills-dir skills`. Web search uses Tavily
when its key is configured, otherwise it tries the optional DuckDuckGo backend.

Default features can make model calls outside the main Run. Memory performs one
digest after each completed Run and another call when a periodic dream is due;
titles and follow-up suggestions also use separate calls. Disable the relevant
features with `--no-memory` or `--no-followups`, or assign auxiliary work to a
cheaper model. See [Memory](memory.md#sharp-edges) for the full cost boundary.

!!! danger "Local use by default"

    Loopback `127.0.0.1` requires no credentials. Binding another address
    requires `--token` or `LOVIA_WEB_TOKEN`; the server generates one when none
    is supplied. A client holding that token can use every Agent capability,
    including file edits and shell execution. Treat it as a password and
    prefer `--readonly` when allowing other devices. For multi-user operation,
    see [Deployment](deployment.md).

## Models and switching

Model profiles are stored in `config.json`. Connection testing makes a real
request to check reachability, authentication, the model list, and the reported
context window. A model ID can still be entered when the endpoint does not
advertise it.

API keys are write-only: the server returns only whether a key is set and a
masked hint, never the complete value. Configuration updates are validated as
a whole, written atomically, and applied without a restart.

A model switch applies from the **next message**, including later messages in
an existing chat, scheduled runs, and background subagents. An in-flight reply
continues on its original model. Vision and auxiliary work such as titles and
follow-up suggestions can be assigned to separate profiles.

The search backend and Tavily key also live in `config.json`. Model
connections, additional profiles, role assignments, and search configuration
have no CLI flags; use Settings or maintain the file directly.

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

Then run:

```bash
lovia web --app app:assistant
```

`--app MODULE:ATTR` accepts one Agent or a `{name: agent}` mapping. For Python
deployment and ASGI integration, see [Web server](web-server.md).

The interface renders GitHub-flavored Markdown, highlighted code, Mermaid, and
inline images. The default Agent is told about these capabilities; a custom
Agent is not. Add `SURFACE_NOTE` when you want the model to use them deliberately:

```python
from lovia.web import SURFACE_NOTE

assistant = Agent(
    name="assistant",
    instructions="Answer clearly.\n\n" + SURFACE_NOTE,
    model="<model>",
)
```

## Images and files

Attachments are stored under the Workspace's `uploads/` directory, while the
message records a relative path. The Agent can therefore open a file through
Workspace Tools even when the model cannot consume it directly.
`--no-workspace` disables this path as well.

Vision configuration controls whether images are sent inline:

- Official OpenAI and Anthropic endpoints are treated as vision-capable by
  default. Other multimodal endpoints need a vision declaration or
  `LOVIA_VISION=1`.
- A text-only main model can assign vision to another profile. With environment
  configuration, use `LOVIA_VISION_MODEL=<vendor>:<model>` and, for a separate
  endpoint, `LOVIA_VISION_BASE_URL` plus `LOVIA_VISION_API_KEY`. The main model
  then receives text through the `see_image` Tool; raw image data does not
  enter its conversation history.

Uploads default to 25 MiB per file (`LOVIA_MAX_UPLOAD_MB`). Named extensions
must match the built-in allowlist; override it with
`LOVIA_UPLOAD_ALLOWED_EXT`, separated by commas or spaces, or use `*` for any.

A file uploaded from the Files panel is not automatically attached to the next
message. Directories such as `tmp/`, `node_modules/`, `venv/`, and
`__pycache__/` are hidden by default. Deleting a chat does not remove files
under `uploads/`; the application or user must clean them up.

Background processes belong to the chat rather than one Run. Finishing a Run
does not stop them; deleting the chat or stopping the Web server reaps them,
and a server restart does not restore them. See
[Workspace](workspace.md#background-processes) for the full lifecycle.

## Useful CLI options

Apart from model and search configuration, options resolve as CLI flag,
environment variable, then default.

| Flag | Environment | Default |
| --- | --- | --- |
| `--host` / `--port` | `LOVIA_HOST` / `LOVIA_PORT` | `127.0.0.1` / `8000` |
| `--token` | `LOVIA_WEB_TOKEN` | Not needed on loopback; generated otherwise |
| `--db` | `LOVIA_DB` | `./.lovia/<agent>.db` |
| `--app MODULE:ATTR` | `LOVIA_APP` | Build the default Agent |
| `--skills-dir` | `LOVIA_SKILLS_DIR` | `./.agents/skills` when present |
| `--memory-dir` / `--no-memory` | `LOVIA_MEMORY_DIR` | `./.lovia/memory` |
| `--workspace`, `--readonly` / `--trusted` / `--no-workspace` | `LOVIA_WORKSPACE`, `LOVIA_WORKSPACE_MODE` | `.` in coding mode |
| `--instructions-file` | `LOVIA_INSTRUCTIONS_FILE` | `AGENTS.md` when present |
| `--max-retries` / `--max-turns` | `LOVIA_MAX_RETRIES` / `LOVIA_MAX_TURNS` | `4` / `50` |
| `--no-followups` | `LOVIA_FOLLOWUPS` | Follow-up suggestions enabled |
| `--no-subagents` | — | Background subagents enabled |
| `--check` | — | Check model configuration and exit |

Use `lovia web --help` for the complete list.

## Questions from the Agent

The default Agent includes the [`ask_human`](built-in-tools.md#ask-a-human)
Tool. A pending question is stored on the server, so refreshes and reconnects
do not lose it. After the default ten-minute timeout, the call is cancelled
and the model continues with a Tool error. This also prevents scheduled and
background work from occupying a run slot indefinitely.

A custom Agent must share one `HumanChannel` between the Tool and the Web app:

```python
from lovia import Agent
from lovia.tools import HumanChannel, ask_human
from lovia.web import serve

channel = HumanChannel()
agent = Agent(name="bot", model="<model>", tools=[ask_human(channel)])
serve(agent, question_channel=channel, question_timeout=600)
```

## Follow-up suggestions

Follow-up suggestions use a separate model call and never enter the Transcript.
Disable them to avoid that extra call with `--no-followups`, or point them at a
cheaper model with `LOVIA_FOLLOWUP_MODEL`. They are opt-in for a custom Agent
served through `create_app()`. Separate endpoint configuration is documented
under [Web server](web-server.md#follow-up-suggestions).

## Run and connection boundaries

Runs are managed by the server. Refreshing or closing the page does not cancel
a Run, and reconnecting resumes from server state. Explicitly stopping a Run
still preserves completed Turns in the Session.

A graceful server shutdown retains the main Run's Checkpoint so it can resume
after restart. Process-local approval and SSE subscription state is not
restored, nor are Shell background processes or background subagents.

Text sent while a Run is active becomes steering input for its next Turn. If
the Run finishes first, the text starts the next Run in order. Attachments are
not queued and must wait for the active Run to finish.

## Background subagents

`lovia web` enables [background subagents](multi-agent.md#background-subagents)
by default; disable them with `--no-subagents`. A default child keeps Skills,
Todo, Tools, and Workspace, but omits Scheduling, Memory, and Subagents, so it
cannot spawn recursively.

A custom Agent must attach the `Subagents` Plugin. `create_app()` wires a
Plugin using its default execution mode into Web supervision; disable that with
`create_app(..., wire_subagents=False)`. An app mounting `build_api_router`
directly must call `wire_subagents(app)` once.

Each background task has an independent Session. When it completes, its report
enters the parent's next Turn if the parent Run is active; otherwise the server
starts a Run to process the report. The browser does not need to remain online.

A task is not owned by the parent Run, so stopping the parent does not stop the
task. Approvals auto-deny after `approval_timeout`; unfinished tasks are not
resumed after a server restart.

## See also

- [Web server](web-server.md) — Python API, lifecycle, and scheduling
- [HTTP API](http-api.md) — build a different front end
- [Tools: approval](tools.md#tool-approval) — approval flow and fail-closed behavior
- Example: [`26_web_serve.py`](../../examples/26_web_serve.py)
