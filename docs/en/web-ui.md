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

Open `http://127.0.0.1:8000`. On first launch the CLI asks for missing model
configuration, verifies it, and can save it to `./.env`.

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

## Attachments — images and files

The composer's **+** button (also drag-drop or paste) uploads images and files
into the Workspace's `uploads/` directory. Each upload is referenced from your
message by its workspace path, so the Agent can open it with its file tools —
this works with any model, and the uploads also appear in the Files panel.
Clicking an attachment keeps you in the app: files open in the Files panel and
images open in a lightbox, rather than a new browser tab or a download.

Images additionally go **inline** to models that can see them:

- **Vision-capable main model.** Official `api.openai.com` / `api.anthropic.com`
  hosts are assumed multimodal. For any other endpoint (a Qwen-VL / DashScope or
  vLLM deployment, or an Anthropic-compatible gateway that may front a text-only
  model), declare it with `LOVIA_VISION=1` so images are sent inline.
- **Text-only main model.** Set `LOVIA_VISION_MODEL=<vendor>:<model>` (e.g.
  `openai:qwen3.7-plus`) to register a `see_image` tool: the main model
  delegates "look at this image" to that vision model and gets back a text
  answer, so the image bytes never enter the main transcript. The `vendor:`
  prefix picks the API dialect — same rule as `LOVIA_MODEL`: `openai:`/bare is
  OpenAI-compatible, `anthropic:` is Anthropic. Its endpoint and key default to
  the `OPENAI_*` / `ANTHROPIC_*` the prefix routes to; set
  `LOVIA_VISION_BASE_URL` / `LOVIA_VISION_API_KEY` when the vision model lives on
  a different endpoint than your main model.

Attachments require a Workspace (the same switch as the Files panel), so
`--no-workspace` hides the **+** button.

Uploads are capped at `LOVIA_MAX_UPLOAD_MB` MiB (default 25) and limited to a
built-in allowlist of common image, document, data, and code extensions. Set
`LOVIA_UPLOAD_ALLOWED_EXT` (comma/space-separated extensions, or `*` for any) to
override it.

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
