# Web UI

The optional browser UI turns any lovia Agent into a local chat application.
It includes streaming text and Tool activity, conversation history with
edit-and-resend and regenerate, titles, approvals, schedules, a memory editor,
image and file attachments, and a read-only Workspace file panel. All browser
assets are bundled, so the page works without a CDN or external font requests.

## Start in one command

```bash
pip install "lovia[web]"
lovia web
```

Open `http://127.0.0.1:8000`. On first launch the CLI asks for whatever model
configuration is missing, checks it against the endpoint, and can save it to
`.lovia/config.env` (owner-only, git-ignored) so it is never retyped.

The default Agent includes `Todo`, optional Skills from `./skills`, Memory in
`./.lovia/memory`, time and HTTP Tools, web search (Tavily when
`TAVILY_API_KEY` is set, else optional DuckDuckGo), scheduling, and a
coding-mode Workspace rooted at the current directory. If `AGENTS.md` exists,
its content becomes the Agent's instructions.

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

## Images and files

Attachments (the composer's **+**, or drag-drop, or paste) are uploaded into the
Workspace's `uploads/` directory and referenced from your message by path, so
the Agent can open them with its file tools whatever model you run —
`--no-workspace` therefore hides the button.

Images additionally go **inline** to models that can see them:

- **Vision-capable main model.** Official `api.openai.com` / `api.anthropic.com`
  hosts are assumed multimodal. Declare any other endpoint (a Qwen-VL / DashScope
  or vLLM deployment, or a gateway that may front a text-only model) with
  `LOVIA_VISION=1`.
- **Text-only main model.** `LOVIA_VISION_MODEL=<vendor>:<model>` (e.g.
  `openai:qwen3.7-plus`) registers a `see_image` Tool: the main model delegates
  "look at this image" and gets back a text answer, so the image bytes never
  enter the main transcript. The `vendor:` prefix picks the API dialect exactly
  as in `LOVIA_MODEL`, and the endpoint and key default to the `OPENAI_*` /
  `ANTHROPIC_*` pair it routes to — override with `LOVIA_VISION_BASE_URL` /
  `LOVIA_VISION_API_KEY` when the vision model lives elsewhere.

Uploads are capped at 25 MiB (`LOVIA_MAX_UPLOAD_MB`) and limited to a built-in
allowlist of common image, document, data, and code extensions
(`LOVIA_UPLOAD_ALLOWED_EXT`, comma/space-separated, or `*` for any).

## Useful CLI options

Every option resolves in this order: command-line flag, environment variable,
`.lovia/config.env`, `./.env` (or the `--env-file` files), then the default.

| Flag | Environment | Default |
| --- | --- | --- |
| `--host` / `--port` | `LOVIA_HOST` / `LOVIA_PORT` | `127.0.0.1` / `8000` |
| `--token` | `LOVIA_WEB_TOKEN` | None on loopback; generated + printed otherwise |
| `--db` | `LOVIA_DB` | `./.lovia/<agent>.db` |
| `--model` | `LOVIA_MODEL` | Asked on first run |
| `--app MODULE:ATTR` | `LOVIA_APP` | Build the default Agent |
| `--skills-dir` | `LOVIA_SKILLS_DIR` | `./skills` when present |
| `--memory-dir` / `--no-memory` | `LOVIA_MEMORY_DIR` | `./.lovia/memory` |
| `--workspace`, `--readonly` / `--trusted` / `--no-workspace` | `LOVIA_WORKSPACE`, `LOVIA_WORKSPACE_MODE` | `.` in coding mode |
| `--instructions-file` | `LOVIA_INSTRUCTIONS_FILE` | `AGENTS.md` when present |
| `--max-retries` / `--max-turns` | `LOVIA_MAX_RETRIES` / `LOVIA_MAX_TURNS` | `4` / `50` |
| `--env-file` | — | `.lovia/config.env`, then `./.env` |

`lovia web --help` lists them all in four groups: model (including the context
window), agent, server, and advanced — output tokens, Provider timeout and
retries, proxy handling, and log level.

## Closing or refreshing the page

Runs are managed by the server, so closing or refreshing the page does not stop
them. Reopen the conversation to catch up and resume live updates. Only the
Stop button cancels a Run; completed turns remain in the Session. Running
sessions are marked in the sidebar and can also be stopped there.

## See also

- [Web server](web-server.md) — Python API, lifecycle, and scheduling
- [HTTP API](http-api.md) — build a different front end
- [Tools: approval](tools.md#tool-approval) — how approval dialogs resolve
- Example: [`26_web_serve.py`](../../examples/26_web_serve.py)
