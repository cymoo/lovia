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
| `run_context` | `None` | Builds each run's `RunContext.deps` from the page's context and the request's identity (see [Run context](#run-context)) |
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
| `empty_title` / `empty_description` | lovia's copy, in the user's language | Blank chat state copy; the description may be a list of short lines |
| `empty_examples` | `()` | Clickable starter prompts on the blank chat state (clicking fills the composer) |
| `login_url` | `None` | Where a signed-out user goes under `auth=` (see [Authentication](#authentication)) |
| `assets` | `None` | Your UI directory: an `index.html` override, stylesheets, images, scripts (see [Customizing the page](#customizing-the-page)) |
| `scripts` | `()` | JS modules the page imports before it starts, e.g. component renderers (see [Extension scripts](#extension-scripts)) |
| `styles` | `()` | Stylesheets loaded after the bundled one |
| `logo` / `favicon` | `None` | An image replacing the sidebar's title / the tab icon |
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

Keep your files in one directory and point `ChatUI(assets=...)` at it.
`lovia web init-ui ui` starts one: an `index.html` listing every block, a
`theme.css` listing every token with its default, and a type-checked
`extension.js` — all commented out, so the scaffold alone changes nothing.

```text
ui/
├── index.html    # optional: extends the bundled page, fills its blocks
├── theme.css
├── logo.svg
├── favicon.png
└── acme.js
```

```python
from lovia.web import ChatUI, create_app

app = create_app(
    agent,
    title="Acme",
    ui=ChatUI(
        assets="ui",
        styles=["theme.css"],
        logo="logo.svg",
        favicon="favicon.png",
        scripts=["acme.js"],
    ),
)
```

lovia serves the directory at `<app>/ui/`. Every file in it is public except
templates (`*.html`) and dotfiles, and each load revalidates it, so an edit
shows on the next reload without a restart. Each name in `scripts`, `styles`,
`logo` and `favicon` is one of:

- a file in `assets` — `"theme.css"`;
- a path on the app — `"/brand/theme.css"`, for files you serve yourself; it
  keeps working under a [path prefix](#serving-under-a-path-prefix);
- a URL — `"https://cdn.example.com/theme.css"`.

Beyond those files, you build on the page through a few public surfaces, each
with its own job:

| Surface | What it's for |
| --- | --- |
| [Template blocks](#template-blocks) | Your own markup at fixed places: links, a control by the composer, a notice |
| [Design tokens](#styling) | Colors, fonts, radii, spacing |
| [`data-part` attributes](#styling) | Targeting a region from your CSS: hiding or restyling it |
| [`data-i18n` keys](#wording-and-languages) | Wording and languages |
| [`window.lovia`](#extension-scripts) and `api.js` | Your scripts; talking to the API |

Everything else — class names, element ids, the JavaScript modules — is
internal and may change between releases.

Two runnable examples put these together: `examples/32_web_branding.py` brands
the page (logo, tokens, blocks, wording, an added language), and
`examples/33_web_extensions.py` extends its behavior through `window.lovia`
(tool views, events, actions).

### Template blocks

An `index.html` in `assets` replaces the page. Extend the bundled one and fill
only the blocks you need:

```html+jinja
{# ui/index.html #}
{% extends "lovia/index.html" %}

{% block sidebar_nav %}
<a href="/help" data-i18n="acme.help">Help center</a>
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
| `head` | End of `<head>`: meta tags, preloads |
| `sidebar_header` | Top of the sidebar; wraps the title (or `logo`), so filling it replaces both — a logo with a name beside it, say |
| `sidebar_nav` | End of the sidebar's navigation, under Settings: links of your own |
| `sidebar_footer` | Bottom of the sidebar: a user menu, a sign-out link |
| `topbar_actions` | Start of the top bar's button group |
| `composer_actions` | In the composer, after the message box: a mode switch, a picker |
| `welcome` | The blank chat state's content; `{{ super() }}` keeps the default title, description and examples. The page restores it on "New chat" |
| `body_end` | After the app's script: scripts of your own |

Links in `sidebar_nav` draw like the bundled items, and the one to the open
[view](#views) shows as current. To reference another file from `assets` in a template, use
`{{ url_for('ui-assets', path='banner.png').path }}` — it carries the path
prefix.

### Styling

The bundled `styles.css` sits in a cascade layer named `lovia`. Any rule in
your own stylesheet (outside a layer) beats every bundled rule, whatever the
selectors' specificity — no `!important` needed.

Retheme by overriding the design tokens at the top of `styles.css`. Each theme
has its own values, so set yours per theme; a token set on `:root` applies to
both. `--accent-hover`, `--accent-soft` and `--accent-text` derive from
`--accent`, so a new brand color is usually just `--accent` — override a tint
only when it needs to differ:

```css
/* ui/theme.css */
[data-theme="light"] { --accent: #6d28d9; }
[data-theme="dark"]  { --accent: #a78bfa; }
:root { --sans: "Inter", system-ui, sans-serif; --radius: 8px; }
```

| Tokens | What they set |
| --- | --- |
| `--bg`, `--surface`, `--surface-2`, `--surface-3`, `--border`, `--border-strong` | Canvas, panels, cards, hover, and their borders |
| `--ink`, `--ink-2`, `--muted`, `--muted-2` | Text, from primary to faint |
| `--accent`, `--accent-hover`, `--accent-soft`, `--accent-text` | The brand color and its tints (the last three derive from `--accent`) |
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
| Views | `view` (the container an open [view](#views) draws in) |
| Conversation | `transcript`, `welcome`, `todo-panel`, `turn` (with `data-role="user"` or `"assistant"`), `tool` (with `data-tool="<tool name>"` and `data-status`: `running`, `awaiting` the user — a question — then `done` or `error`), `activity` (two or more consecutive reasoning / tool steps folded under one summary line; it closes once the reply's text starts), `message-actions` (a message's [actions](#message-actions); each button has `data-action="<id>"`) |
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

### Extension scripts

`ChatUI(scripts=[...])` lists ES modules the page imports **before it starts**,
so whatever they register — a [component renderer](#ui-components), a
[tool view](#tool-views) — is in place for the first render. They reach the
page through `window.lovia`, its one public script API; the other modules are
internal.

| `window.lovia` | Description |
| --- | --- |
| `version` | The lovia version serving the page, for feature checks |
| `ready` | A promise that settles once the app's start is over: the page is wired up, the first chat-list request and the chat in the URL have loaded or failed, and the [view](#views) in the URL is open. It stays pending if the page finds the user signed out |
| `state` | A frozen snapshot: `sessionId` (null for a new chat), `agent`, `theme`, `lang`, `busy` (a run is streaming in), `view` (the open [view](#views), or null). Read it again for the current one |
| `on(event, fn)` | Listen for an [event](#events); returns a function that stops listening |
| `send(text)` | Send `text` as the user; while a run streams it is queued for the next turn |
| `setDraft(text)` | Put `text` in the composer for the user to edit and send |
| `newChat()` / `openSession(id)` | Show a new chat / chat `id`, closing any view |
| `toast(text, { type })` | A transient notice; `type` is `"info"` (default) or `"error"` |
| `api` | The page's API client — the [path prefix](#serving-under-a-path-prefix) and sign-in handling built in |
| `fetch(path, init)` | `fetch` for your own routes: `path` starts with `/` and gets the path prefix; a 401 gets the page's sign-in handling |
| `t(key, params)` | A UI string in the page's language, including your [own keys](#wording-and-languages) |
| `registerRenderer(kind, renderer)` / `getRenderer(kind)` | [UI components](#ui-components) |
| `registerToolView(name, view)` | [Tool views](#tool-views) |
| `registerMessageAction(id, action)` / `refreshMessageActions()` | [Message actions](#message-actions) |
| `registerView(id, view)` / `openView(target)` / `closeView()` | [Views](#views) |
| `provideContext(key, fn)` | Add `key` to the context runs start with, read from `fn()` at send time — see [Run context](#run-context) |

```js
// ui/acme.js
const { lovia } = window;

lovia.ready.then(() => {
  console.info(`Acme extensions on lovia ${lovia.version}`);
});

// A button the template put in the composer_actions block.
document.getElementById('acme-summarize')?.addEventListener('click', () => {
  lovia.send(lovia.t('acme.summarizePrompt'));
});
```

Don't `await lovia.ready` at the top level of a script: the page waits for your
script before it starts, so the two would wait on each other until the
5-second limit below. A script that fails to load shows an error toast, and the
page starts without it. One still loading after 5 seconds stops holding up the
start; it keeps loading and registers when it lands. Scripts are your own code,
with the page's full privileges, so self-host third-party code in `assets`
rather than loading it from someone else's CDN.

For editor completion and type checks, reference the declarations shipped with
lovia (the path is under your environment's `site-packages`):

```js
// @ts-check
/// <reference path="…/site-packages/lovia/web/static/lovia.d.ts" />
```

### Events

Events describe what the page on screen does. A listener that throws is logged
and doesn't stop the others.

| Event | Payload | When |
| --- | --- | --- |
| `session` | `{ sessionId }` | Another chat is on screen (`null`: a new one); also when a new chat gets saved |
| `agent` | `{ agent }` | The user picked another agent |
| `run-start` | `{ sessionId }` | A run began streaming into the chat: sent, or reattached after a reload |
| `run-end` | `{ sessionId, status, usage }` | The chat stopped streaming it. `status`: `done` (answered), `error`, `cancelled` (stopped by the user), or `detached` (the user switched chats or the connection dropped; the run may go on server-side). `usage` is the answered run's token usage, else `null` |
| `theme` | `{ theme }` | Light or dark took effect |
| `view` | `{ view }` | A [view](#views) opened (`'exec/perf'`), or the chat came back (`null`) |

```js
lovia.on('run-end', ({ status, usage }) => {
  if (status === 'done') analytics.track('reply', { tokens: usage?.total_tokens });
});
```

### Tool views

Every tool call shows as a card: its name and arguments on one line, the
parameters and result inside. `registerToolView` changes how a tool's cards
read — useful for the tools you don't control (MCP servers, say), and for
end-user deployments where `order_status(id: …)` means nothing to the reader.
lovia's own tools already read this way — "Read file a.md", "$ ls -la",
"Web search “…”" — and a view you register, the `'*'` fallback included, takes
precedence over theirs:

```js
// One line in place of name(args), for every tool without a view of its own;
// null hands over to the next view (lovia's line for its own tools), and in
// the end keeps name(args).
const labels = { order_status: 'Looking up an order' };
lovia.registerToolView('*', { summary: (tool) => labels[tool.name] ?? null });

lovia.registerToolView('web_search', {
  summary: (tool) =>
    tool.status === 'done' ? `Searched “${tool.args?.query}”` : `Searching “${tool.args?.query}”…`,
  // Draw the card's body; called again on every change of `tool`.
  render(el, tool) {
    if (tool.status !== 'done') return;
    const list = document.createElement('ol');
    for (const hit of JSON.parse(tool.result).results ?? []) {
      const link = document.createElement('a');
      link.href = hit.url;
      link.target = '_blank';
      link.textContent = hit.title; // never innerHTML: tool output is untrusted
      const item = document.createElement('li');
      item.append(link);
      list.append(item);
    }
    el.replaceChildren(list);
  },
});
```

| `view` field | Description |
| --- | --- |
| `summary(tool)` | The card's one-line summary instead of `name(args)`; `null` or `undefined` falls through to the `'*'` view, then lovia's own line, then `name(args)`. Hovering shows the call it stands for |
| `render(el, tool, ctx)` | Draw the card's body into `el`. Same contract as a [component renderer](#ui-components) (`ctx`, an optional cleanup function), and the raw result stays one "Source" toggle away |
| `snapshot(tool, ctx)` | Static HTML for the chat's HTML export |

- `tool` is a frozen snapshot of the call: `id`, `name`, `args` (parsed; absent
  when over 256 KB or unparsable), `status` (`running`, `awaiting` the user,
  `done`, `error`), `result` (the output text, once settled), `isError`,
  `images`. A view's functions run again whenever it changes.
- The view for a tool's name wins over the `'*'` view, field by field: a view
  with only `summary` still gets the `'*'` view's `render`.
- A result that is itself a [UI component](#ui-components) keeps drawing as
  that component; the view's `summary` still applies.
- Hiding a tool's cards is CSS's job (`[data-part="tool"][data-tool="…"]`, see
  [Styling](#styling)), not a view's.
- Kinds starting with `lovia.` are lovia's own: don't register renderers under
  them.

### Message actions

`registerMessageAction(id, action)` adds a button to the messages' footers,
beside copy: rate an answer, report it, save it to notes. lovia keeps none of
that data — the action calls a route of your own:

```js
const ratings = new Map(); // ref → 'up' | 'down', for the chat on screen

lovia.registerMessageAction('rate-up', {
  label: 'Good answer',
  icon: '👍',
  when: (msg) => msg.role === 'assistant' && msg.ref !== null,
  pressed: (msg) => ratings.get(msg.ref) === 'up',
  async run(msg) {
    const res = await lovia.fetch('/feedback', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ session_id: msg.sessionId, ref: msg.ref, rating: 'up' }),
    });
    if (!res.ok) throw new Error('Could not save the rating'); // shown as a toast
    ratings.set(msg.ref, 'up');
    lovia.refreshMessageActions(); // draw it pressed
  },
});
```

| `action` field | Description |
| --- | --- |
| `label` | The tooltip and accessible name |
| `icon` | A built-in icon name, SVG markup, or text such as an emoji; the label when absent |
| `when(msg)` | Whether `msg` shows the action; every message when absent |
| `pressed(msg)` | Draw the button pressed (`aria-pressed`), e.g. a rating already given |
| `run(msg)` | Called on a click; may be async, and the button is disabled until it settles. An error it throws shows as a toast |

- `msg` is a frozen snapshot: `sessionId`, `role` (`user` or `assistant`),
  `text` (Markdown, what copy takes), and `ref`.
- `ref` names a user message — `"<run_id>:<n>"` — and a reply carries the
  `ref` of the message it answers, so `(sessionId, ref)` is the key for data
  you keep about a message. It survives reloads, and it never names another
  message: rewinding cuts a message's ref out for good, so an edited question
  or a regenerated answer gets a new one, and an old rating never lands on new
  text.
- `ref` is `null` until the server names the message: when its run finishes,
  or on the next load of the chat for a run that failed or was stopped (and
  for every run when the chat store keeps no checkpoints).
  `when: (msg) => msg.ref !== null` holds an action back until then; one that
  needs no ref (quote, translate) shows at once.
- Actions show on settled messages: none on a reply still streaming in or a
  message still queued.
- `when` and `pressed` are asked when a footer is drawn. After the state they
  read changes (a rating saved, ratings fetched for the chat that came on
  screen), call `lovia.refreshMessageActions()`. Registering under an `id`
  again replaces the action; `null` removes it.

`examples/35_web_feedback.py` is a complete version: 👍 / 👎 on answers kept
in SQLite by its own routes, a report page, and a Quote action.

### Views

A view is a page of your own in the main area, in place of the chat: a
dashboard, a settings form, a list of records. Register it, then link to it —
a plain link opens it:

```html+jinja
{% block sidebar_nav %}
<a href="?view=dashboard">Dashboard</a>
{% endblock %}
```

```js
lovia.registerView('dashboard', {
  title: 'Dashboard',
  async render(el, ctx) {
    const res = await lovia.fetch('/acme/stats');
    el.replaceChildren(drawStats(await res.json(), { tab: ctx.path }));
    const off = lovia.on('theme', redrawCharts);
    return off; // called when the view closes or draws again
  },
});
```

| `view` field | Description |
| --- | --- |
| `title` | The top bar's title while the view is open |
| `render(el, ctx)` | Draw the view into `el`, on each opening and each change of its path. May be async; return a cleanup function. `ctx.path` is what follows the id in `?view=<id>/<path>` (`''` when nothing does) |

- **A view covers the chat, it doesn't leave it.** The chat stays current
  underneath (`state.sessionId`), a run keeps streaming, and the view's
  state lives in the URL beside the chat's: `?session=…&view=dashboard`.
- **Links.** A plain click on a link to `?view=<id>[/<path>]` opens the view
  in place; a middle or ⌘/Ctrl click opens it in a new tab, which starts on
  that view. `?view=` (empty) returns to the chat. Write them relative, as
  above, so they hold under a [path prefix](#serving-under-a-path-prefix).
  Links inside messages are left alone: model output doesn't navigate.
- **Back to the chat.** Clicking any chat — the current one too — starting
  a new chat, `lovia.openSession`, `lovia.newChat` and `lovia.closeView()`
  close the view. `lovia.send` doesn't: the message goes to the chat
  underneath. While a view is open and the chat starts waiting on the user
  (an approval, a question), the page says so. On a phone, opening a view
  or a chat closes the sidebar drawer.
- **What shows.** The top bar stays, with the view's title and your
  `topbar_actions`; the parts that belong to the chat hide. The view draws in
  `[data-part="view"]`, which you may style. A link in `sidebar_nav` to the
  open view gets `aria-current="page"` (compared by id, so `?view=exec`
  stays current on `exec/perf`).
- **Lifecycle.** The previous cleanup runs before each draw. A render that
  throws shows a notice in the view; an async one that lands after the view
  was left is cleaned up at once. Registering under an `id` again redraws it
  if open; `null` removes it. A theme switch doesn't redraw a view (it would
  lose form input and scroll), so listen for `theme` if yours follows it.
- `openView(target)` throws for an id with no view; a link to one, or one in
  the URL at start, shows a notice instead. Ids use `a-z`, `0-9` and `-`;
  ids starting `lovia` are reserved.

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
// ui/kpis.js
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
app = create_app(agent, ui=ChatUI(assets="ui", scripts=["kpis.js"]))
```

The renderer gets the element to draw into, the props, and a context:

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

## Run context

Tools often need to know who is asking, and what the user picked on the page —
the knowledge base in a dropdown by the composer, say. `create_app(run_context=fn)`
builds each run's [`RunContext.deps`](agents.md#inject-application-dependencies)
from both:

```python
from dataclasses import dataclass

from fastapi import HTTPException, Request
from lovia import RunContext, tool
from lovia.web import RunTrigger, create_app


async def auth(request: Request) -> None:
    request.state.user = await users.from_session_cookie(request)  # your identity


@dataclass
class Deps:
    user: str
    kb: str


def run_context(trigger: RunTrigger) -> Deps:
    user = trigger.request.state.user if trigger.request else "scheduler"
    kb = trigger.client.get("kb", "docs")
    if kb not in KNOWLEDGE_BASES:
        raise HTTPException(400, f"unknown knowledge base {kb!r}")
    return Deps(user=user, kb=kb)


@tool
async def search(ctx: RunContext[Deps], query: str) -> str:
    """Search the user's knowledge base."""
    return await KNOWLEDGE_BASES[ctx.deps.kb].search(query, user=ctx.deps.user)


app = create_app(agent, auth=auth, run_context=run_context)
```

On the page, an extension script supplies the client side with
`lovia.provideContext(key, fn)`; `fn()` is read each time a message (or a new
schedule) is sent:

```js
const kb = document.getElementById('acme-kb'); // a <select> in the composer_actions block
window.lovia.provideContext('kb', () => kb.value);
```

`run_context` is called once per run — sync or async — and what it returns is
the run's `ctx.deps`, shared by every leg of an auto-continued run. It runs
before anything about the run is written, so raising an `HTTPException`
refuses the run and leaves nothing behind: a chat message gets the error, with
no new chat created and an interrupted run still resumable; a schedule's fire
is recorded as a failed run, and the schedule moves on to its next time. Keep
it quick — every message waits for it before its run starts. `trigger`
carries:

| Field | Description |
| --- | --- |
| `client` | The page's context (`provideContext`'s keys): UI state. **The browser controls it — never derive identity or permissions from it** |
| `request` | The request that started the run, after `auth=` ran: identity lives here (`request.state`). `None` when nobody requested the run |
| `source` | Why the run started: `user` (a message, or a reconnect resuming a run), `schedule:<id>`, `subagent:<session>`, `subagent-report:<task>` |
| `resumed` | A reconnect is resuming the run from its checkpoint |
| `session_id`, `agent` | The chat, and the agent about to run |

Every run gets a context, including the ones nobody sent:

- **Resume after a restart.** A checkpoint keeps no context, so the server
  stores the run's `client` beside its checkpoint pointer and clears it when
  the run ends; the resume gets it back, with the reconnecting `request`.
- **Schedules.** A schedule is a deferred message: one made from the
  Schedules dialog stores the page's context at that moment, one the agent
  makes with `schedule_run` stores its own run's. Each fire gets it, with
  `request=None`.
- **Subagent tasks and their reports** get the latest context of the chat
  they work for, with `request=None`.

A `client` is capped at 64 KB of JSON. Without `run_context`, runs have no
context (as with the core `Runner`), and a request that carries one logs a
warning.

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

Chat Runs — streamed or blocking — are server-owned tasks. An SSE subscriber
may disconnect and reattach without cancelling work, and a blocking
`/api/chat` client that disconnects leaves its Run to finish into the chat.

- **User cancellation** finalizes completed turns into the Session, removes
  dangling Tool calls, and clears the checkpoint.
- **Server shutdown** cancels Runs cooperatively but keeps checkpoints, so a
  client can reconnect and resume after deployment.
- **Capacity** is bounded by `max_background_runs`; new starts receive HTTP 429
  while full.
- **Blocking `/api/chat`** waits for its Run and answers with the outcome
  ([details](http-api.md#blocking-turns)). With no client to ask, its gated
  Tool calls go to the Agent's `approval_handler`, else are denied.

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
