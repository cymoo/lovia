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
Server options such as `log_level`, `ssl_certfile`, `workers`, and `root_path`
([behind a proxy](#serving-under-a-path-prefix)) pass through to uvicorn. To
run the app under another ASGI server, skip `serve()`.

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
| `ui` | `True` | `False` serves the API only; a `ChatUI` customizes the bundled page (below) |
| `cors_origins` | `None` | Allowed browser origins; unset sends no CORS headers |
| `token` / `auth` | `None` | Bearer-token guard for business API routes, or your own FastAPI dependency (see below) |
| `lifespan` | `None` | Your own resources' lifespan, as for `FastAPI(lifespan=...)` (see below) |
| `title` | `"lovia"` | Page title and the API's reported title |

`create_app` sets the app's FastAPI `lifespan` itself, to start and stop its
scheduler and wind down live runs. Pass your own as `lifespan=` to open
resources your tools use — a database pool, an HTTP client — with the app.
Yours wraps lovia's: the resources are up before the first run and close only
after live runs wound down. State you yield reaches `request.state`.

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    await db.connect()
    yield
    await db.close()

app = create_app(agent, lifespan=lifespan)
```

`ChatUI` options, for `ui=ChatUI(...)`:

| Option | Default | Description |
| --- | --- | --- |
| `empty_title` / `empty_description` | lovia defaults | Blank chat state copy; the description may be a list of short lines |
| `empty_examples` | `()` | Clickable starter prompts on the blank chat state (clicking fills the composer) |
| `login_url` | `None` | Where a signed-out user goes under `auth=` (see [Authentication](#authentication)) |
| `templates` | `None` | A directory of your own templates (see [Customizing the page](#customizing-the-page)) |
| `scripts` | `()` | JS modules loaded after the app's own, e.g. component renderers (see [UI components](#ui-components)) |
| `messages` | `{}` | UI strings per language: overrides, extra languages, and keys for your own template (see [Wording and languages](#wording-and-languages)) |
| `default_lang` | `None` | The language for a user who hasn't picked one in Settings; `None` follows the browser |

```python
from lovia.web import ChatUI, create_app

app = create_app(
    agent,
    title="Acme",
    ui=ChatUI(empty_title="Ask Acme", empty_examples=["Summarize today's tickets"]),
)
```

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

## Customizing the page

You build on the bundled page through a few public surfaces, each with its own
job:

| Surface | What it's for |
| --- | --- |
| [Template blocks](#template-blocks) | Your own markup at fixed places: a logo, links, a control by the composer |
| [Design tokens](#styling) | Colors, fonts, radii, spacing |
| [`data-part` attributes](#styling) | Targeting a region from your CSS: hiding or restyling it |
| [`data-i18n` keys](#wording-and-languages) | Wording and languages |
| `api.js` and `window.lovia.registerRenderer` / `getRenderer` | Talking to the API; [UI components](#ui-components) |

Everything else — class names, element ids, the JavaScript modules — is
internal and may change between releases.

### Template blocks

`ChatUI(templates="ui")` points at a directory of your own Jinja templates.
An `index.html` there replaces the page. Extend the bundled one and fill only
the blocks you need:

```html+jinja
{# ui/index.html #}
{% extends "lovia/index.html" %}

{% block head %}
<link rel="stylesheet" href="{{ url_for('brand', path='theme.css').path }}">
{% endblock %}

{% block sidebar_header %}
<img class="acme-logo" src="{{ url_for('brand', path='logo.svg').path }}" alt="Acme">
{% endblock %}

{% block sidebar_nav %}
<a class="sidebar-nav-item" href="/help" data-i18n="acme.help">Help center</a>
{% endblock %}

{% block welcome %}
{{ super() }}
<p class="acme-notice">Scheduled maintenance tonight at 22:00.</p>
{% endblock %}

{% block sidebar_footer %}
<a class="signout" href="/logout">Sign out</a>
{% endblock %}
```

| Block | Where it renders |
| --- | --- |
| `head` | End of `<head>`: stylesheets, meta tags |
| `sidebar_header` | Top of the sidebar; wraps the title, so filling it replaces the title (with a logo, say) |
| `sidebar_nav` | End of the sidebar's navigation, under Settings: links of your own |
| `sidebar_footer` | Bottom of the sidebar: a user menu, a sign-out link |
| `topbar_actions` | Start of the top bar's button group |
| `composer_actions` | In the composer, after the message box: a mode switch, a picker |
| `welcome` | The blank chat state's content; `{{ super() }}` keeps the default title, description and examples. The page restores it on "New chat" |
| `body_end` | After the app's script: scripts of your own |

The example's link borrows the bundled `sidebar-nav-item` look. Class names are
internal, so give it a class of your own instead if you'd rather not track them
across releases.

Serve your own files from a static mount on the app, and link them with
`url_for(...).path` so they keep working under a
[path prefix](#serving-under-a-path-prefix):

```python
from fastapi.staticfiles import StaticFiles

app = create_app(agent, ui=ChatUI(templates="ui"))
app.mount("/brand", StaticFiles(directory="brand"), name="brand")
```

### Styling

The bundled `styles.css` sits in a cascade layer named `lovia`. Any rule in
your own stylesheet (outside a layer) beats every bundled rule, whatever the
selectors' specificity — no `!important` needed.

Retheme by overriding the design tokens at the top of `styles.css`. Each theme
has its own values, so set yours per theme; a token set on `:root` applies to
both:

```css
/* brand/theme.css */
[data-theme="light"] { --accent: #6d28d9; --accent-hover: #5b21b6; --accent-soft: #f1ebfe; }
[data-theme="dark"]  { --accent: #a78bfa; --accent-hover: #c4b5fd; --accent-soft: #2a2240; }
:root { --sans: "Inter", system-ui, sans-serif; --radius: 8px; }
```

| Tokens | What they set |
| --- | --- |
| `--bg`, `--surface`, `--surface-2`, `--surface-3`, `--border`, `--border-strong` | Canvas, panels, cards, hover, and their borders |
| `--ink`, `--ink-2`, `--muted`, `--muted-2` | Text, from primary to faint |
| `--accent`, `--accent-hover`, `--accent-soft`, `--accent-text` | The brand color and its tints |
| `--user-surface`, `--user-border` | The user's message bubble |
| `--chart-1` … `--chart-8` | Chart series colors |
| `--warn`, `--danger`, `--ok` | Status colors |
| `--sans`, `--mono` | Font stacks |
| `--radius-xs` … `--radius-xl` | Corner radii |
| `--sidebar-w`, `--content-w` | Sidebar width, the transcript's reading width |

To target a region, select its `data-part`:

```css
[data-part="export"] { display: none; }                                   /* no Export menu */
[data-part="tool"][data-tool="read_process_output"] { display: none; }   /* hide one tool's cards */
[data-part="turn"][data-role="user"] .bubble { border-radius: 4px; }
```

| Region | `data-part` |
| --- | --- |
| Sidebar | `sidebar`, `sidebar-header`, `agent-switcher`, `sidebar-nav`, `new-chat`, `memory`, `schedules`, `settings`, `sessions-label`, `sessions-search`, `sessions` |
| Top bar | `topbar`, `chat-title`, `topbar-actions`, `tasks`, `export`, `files-toggle` |
| Conversation | `transcript`, `welcome`, `todo-panel`, `turn` (with `data-role="user"` or `"assistant"`), `tool` (with `data-tool="<tool name>"`) |
| Composer | `composer`, `attach`, `context-ring` |
| Elsewhere | `files-panel`; in Settings, `language` (the language row) |

Hiding a tool's cards never hides an approval request: those render apart from
the card.

### Wording and languages

The UI ships English and Chinese. `ChatUI(messages=...)` changes its wording,
adds languages, and labels your own template's elements:

```python
ui = ChatUI(
    default_lang="zh",
    messages={
        "en": {"composer.placeholder": "Ask Acme anything…", "acme.help": "Help center"},
        "zh": {"composer.placeholder": "问问 Acme…", "acme.help": "帮助中心"},
        "ja": {"composer.placeholder": "何でも聞いてください…", "nav.newChat": "新しいチャット"},
    },
)
```

- Keys are the ones in `lovia/web/static/js/i18n.js` (`nav.newChat`,
  `composer.placeholder`, …). A key you set under one of the UI's own prefixes
  that the UI doesn't have — a typo, or a string a release renamed — is logged
  as a warning in the browser console.
- A language the UI doesn't ship (`ja` above) becomes selectable in Settings;
  a string it lacks falls back to English.
- Keys under a prefix of your own (`acme.help`) label your template's elements
  through `data-i18n` (text), `data-i18n-title`, `data-i18n-aria` and
  `data-i18n-placeholder`. The element's own text stays when the active
  language has no string for its key.
- `default_lang` applies until the user picks a language in Settings, where
  "Automatic" returns to it. To keep users on one language, set it and hide
  the picker: `[data-part="language"] { display: none; }`.

## UI components

A tool result can render as UI — a sortable table, a KPI strip — instead of
text. The tool returns `ui_result(kind, props)`, and a renderer registered for
`kind` draws it inside the tool card, live and when the chat is reopened.

```python
from lovia import tool
from lovia.web import ui_result

@tool
def top_products(n: int = 10) -> str:
    """Best sellers, shown to the user as a table."""
    rows = query_top_products(n)
    return ui_result("table", {"columns": ["sku", "units"], "rows": rows})
```

Three kinds are built in: `table` (above, see [Tables](web-ui.md#tables)),
`chart` (see [Charts](web-ui.md#charts)), and `html`, a sandboxed page (see
[Pages](web-ui.md#pages)). The numbers in that table come straight from the
query; the model never writes them out.

The envelope is the tool's output, so the model sees the props as well. When
the call's own arguments already are the data (a presentational tool), pass
`from_args=True` — or a field name — instead of `props`, so the data isn't
echoed back a second time. Bulk data stays out of the model's context with
[`put_ui_data`](#bulk-data). Keep an envelope under the Agent's `max_tool_output_chars`
(200K characters by default): a longer output is truncated, which breaks its
JSON, and the card shows it as text instead.

For a kind of your own — say `return ui_result("kpis", {"items": [...]})` —
write a renderer: a JS module loaded with `ChatUI(scripts=[...])`:

```js
// ext/kpis.js
window.lovia.registerRenderer('kpis', {
  render(el, props, ctx) {
    for (const item of props.items) {
      const card = document.createElement('div');
      card.className = 'kpi';
      card.textContent = `${item.label}: ${item.value}`; // never innerHTML
      el.append(card);
    }
    // Optionally return a cleanup function (observers, timers, chart instances).
  },
});
```

```python
from fastapi.staticfiles import StaticFiles

app = create_app(agent, ui=ChatUI(scripts=["ext/kpis.js"]))
app.mount("/ext", StaticFiles(directory="ext"))
```

A relative script URL follows a [path prefix](#serving-under-a-path-prefix);
an absolute one is used as is. The renderer gets the element to draw into, the
props, and a context:

| `ctx` field | Description |
| --- | --- |
| `theme` / `lang` | `'light'` or `'dark'`; `'en'` or `'zh'`. On a theme switch the component is cleaned up and rendered again |
| `basePath` | The mount prefix, for requests to your own endpoints |
| `sessionId` / `callId` | The chat and the tool call the component belongs to |
| `live` | `true` when the result just arrived, `false` when a reopened chat replays it, e.g. to hold back something costly until a click |
| `send(text)` | Post a user message, e.g. from a "drill into March" button. It works only inside a real click or keypress, and returns `false` otherwise |

`render` may be async. If it throws, the card says so, and the raw output
stays behind the card's Source toggle. A kind with no renderer shows a notice
until one registers, so load order doesn't matter. Registering a kind again
replaces its renderer.

The chat's HTML export is a standalone file with no scripts, so `render`
can't draw there. Add a `snapshot(props, ctx)` that returns the component as
static HTML (sync or async; the same `ctx`, without `send`). The export
sanitizes it. Without a snapshot, the export shows the raw output. The
built-in table exports every row as a plain table, and the chart exports as
an SVG image.

```js
window.lovia.registerRenderer('kpis', {
  render(el, props, ctx) { /* … */ },
  snapshot(props) {
    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
    return props.items.map((i) => `<p><b>${esc(i.label)}</b>: ${esc(i.value)}</p>`).join('');
  },
});
```

!!! warning "Props are untrusted"
    Props come from the model, tools, MCP servers, or remote content. Write
    them with `textContent` (or sanitize with the page's `DOMPurify`), never
    as HTML.

Registering a built-in kind replaces it: your own `chart` renderer can draw
charts with another library. `examples/31_web_components.py` replaces the
built-in `table` with a version that adds an "Ask" button to each row, which
posts a follow-up through `ctx.send`.

### Bulk data

The envelope is part of the model's context, turn after turn. For a result
too big for that — a 10,000-row query — store the full props with the chat,
and put a preview in the envelope:

```python
from lovia import tool
from lovia.web import put_ui_data, ui_result

@tool
async def run_query(sql: str) -> str:
    """Run a read-only query and show the result as a table."""
    columns, rows = await db.fetch(sql)
    ref = await put_ui_data({"columns": columns, "rows": rows})
    preview = {"columns": columns, "rows": rows[:20], "row_count": len(rows)}
    return ui_result("table", preview, data=ref)
```

The model reads only the preview, so give it what it needs to talk about rows
it hasn't seen, such as a row count or totals. The page fetches the full props
from `GET /api/sessions/{id}/ui-data/{ref}`, behind the same auth as the rest
of the API, and hands them to the renderer. Any kind works this way, built-in
or your own, with no change to its renderer, and the HTML export draws the
full data too.

- The data lives as long as its chat: deleting the chat deletes it. If it is
  gone, the card shows a notice and then the preview (only the notice when
  the preview is `None`).
- Outside a `lovia web` run, such as a script or an eval, `put_ui_data`
  returns `None`, and the envelope is just the preview.
- A stored value is limited to 8 MiB of JSON. Larger values raise a tool error,
  so the model can narrow the query.

### Wrapping a renderer

`window.lovia.getRenderer(kind)` returns the renderer registered for `kind` —
its `{ render, snapshot }` — or `undefined`, so one renderer can build on
another. The built-in renderers are registered before your scripts run. For
example, a compact kind of your own can be drawn by the built-in chart, live
and in the export:

```js
const chart = window.lovia.getRenderer('chart');
// {label, points: [[x, y], ...]} → an ECharts option.
const option = (p) => ({
  title: { text: p.label },
  dataset: { source: p.points },
  xAxis: { type: 'category' },
  yAxis: {},
  series: [{ type: 'line' }],
});

window.lovia.registerRenderer('trend', {
  render: (el, props, ctx) => chart.render(el, option(props), ctx),
  snapshot: (props, ctx) => chart.snapshot(option(props), ctx),
});
```

The renderer you get is the one registered at the time of the call, so a
wrapper may also replace the kind it wraps — re-register `chart` around the
built-in one, say — without calling itself.

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

**Signing in.** The bundled page can't show your login form, so tell it where
the form lives. With `ui=ChatUI(login_url="/login?next={next}")`, an API call
your dependency answers with 401 sends the browser there, `{next}` replaced by
the URL-encoded path (and query) of the current page. Without `login_url` the
page only says the user is signed out. A burst of 401s acts once, and the
token check's own 401 (code `server_token`) keeps its token prompt.

`create_app()` alone enables no authentication; under another ASGI server,
pass `token` or `auth` yourself. `serve()` only judges apps built by
`create_app()` — an app of your own that mounts `build_api_router` keeps its
own middleware and is run as is.

## Serving under a path prefix

Behind a reverse proxy that serves the app at `/lovia/` and strips the prefix
before forwarding, tell uvicorn the prefix:

```nginx
location /lovia/ {
    proxy_pass http://127.0.0.1:8000/;   # the trailing slash strips /lovia
}
```

```python
serve(app, root_path="/lovia")           # or: lovia web --root-path /lovia
```

The page takes the prefix from each request, so its assets, API calls, event
stream, and token cookie all stay under it; two instances under one host keep
separate tokens. Asset URLs carry no scheme or host, so a proxy that
terminates TLS never turns them into blocked `http://` loads. The server
itself still answers at the root: open the page through the proxy.

To host a `create_app()` app inside an existing FastAPI app instead, mount it
([HTTP API](http-api.md#mounting-a-create_app-app)); the prefix works the same
way.

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
