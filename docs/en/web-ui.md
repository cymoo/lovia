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

Everything the server keeps lives in one directory, lovia's **home** —
`~/.lovia` unless `--home DIR` names another. Nothing is read from the
directory the server was started in, which only decides where new chats work:

| In the home | What |
| --- | --- |
| `config.json` | Models, web search, global skill directories, and the [agents](#agents) |
| `lovia.db` | Chat history (`--db FILE` puts it elsewhere) |
| `memory/` | Long-term memory: notes about you, and per-project notes and history under `scopes/` |
| `workspace/` | Where chats that belong to no project work |

Separate setups — a test instance, unrelated clients — each take their own
`--home`. The home is owner-only on platforms with Unix permissions and holds
a `*` `.gitignore`, so a home inside a repository is never committed. It must
be lovia's own directory: a repository's root, or a directory whose
`.gitignore` doesn't ignore everything, is refused before anything is written.

Before 0.9.179 a `./.lovia/config.json` in the launch directory replaced the
user's file, a `./.lovia/lovia.db` there was kept in use, and memory defaulted
to `./.lovia/memory`. None of that is read now; `lovia web --home ./.lovia`
runs exactly as such a launch did, and the server logs this when it finds a
`./.lovia/config.json` or `./.lovia/lovia.db` without `--home` (the latter
also with how to move those chats into the home instead).

The default Agent includes Todo, Memory, time and HTTP Tools, web search,
scheduling, and a coding-mode Workspace rooted at the current directory. From
the directory each chat works in, on every run, it reads:

- `AGENTS.md` as Agent instructions — see [below](#project-instructions);
- `.agents/skills` as the project's own skills, ahead of the global skill
  directories (`~/.agents/skills` by default; Settings → Skills).

So a chat in another project follows that project's `AGENTS.md` and skills.
Web search uses Tavily when its key is configured, otherwise it tries the
optional DuckDuckGo backend.

Default features can make model calls outside the main Run. Memory performs one
digest after each completed Run and another call when a periodic dream is due;
titles and follow-up suggestions also use separate calls. Turn memory or
follow-ups off for an agent in **Settings → Agents**, or assign auxiliary work to a
cheaper model. See [Memory](memory.md#sharp-edges) for the full cost boundary.

!!! danger "Local use by default"

    Loopback `127.0.0.1` requires no credentials. Binding another address
    requires `--token` or `LOVIA_WEB_TOKEN`; the server generates one when none
    is supplied. A client holding that token can use every Agent capability,
    including file edits, shell execution, other directories and remote
    workspaces — the bind address changes nothing else. Treat it as a password
    and narrow the server with `--readonly`, `--lock-workspace` or
    `--no-workspace` when allowing other devices. For multi-user operation,
    see [Deployment](deployment.md).

### Project instructions

In a Git repository a chat loads every `AGENTS.md` from the repository's
root (the first directory up that holds a `.git`, a worktree's or
submodule's `.git` file included) down to its own directory, outermost
first, each preceded by a line naming its path. Outside a repository, or at
its root, it loads the directory's own `AGENTS.md` as it is. The log names
every file loaded.

- **Size.** 256 KB in all, filled from the chat's directory outwards: a file
  that does not fit is left out whole, and the log says which.
- **Permissions.** These files are the project's instructions, not something
  the agent explores, so the permission mode's default for reads outside the
  folder does not apply: Read-only still loads the repository's rules above
  the chat. Explicit rules do — the built-in credential stores, the server's
  own `config.json`, a deployment's denied paths — judged where a file's
  links lead, so an `AGENTS.md` pointing at a secret is left out.

## Models and switching

Model profiles are stored in `config.json` and edited in **Settings →
Models**; the web-search service has its own page, **Settings → Web search**. Connection testing makes a real
request to check reachability, authentication, the model list, and the reported
context window. A model ID can still be entered when the endpoint does not
advertise it.

API keys are write-only: the server returns only whether a key is set and a
masked hint, never the complete value. Configuration updates are validated as
a whole, written atomically, and applied without a restart.

A model switch applies from the **next message**, including later messages in
an existing chat, scheduled runs, and background subagents. An in-flight reply
continues on its original model. The model chip in the composer sets the
model of the chat's [agent](#agents), and that agent's alone — or puts it back
on the default model. The default, which every agent without a model of its
own follows, changes only in Settings → Models (**Make default**). Vision and auxiliary
work such as titles and follow-up suggestions can be assigned to separate
profiles.

**Extra request fields** (Advanced) is a JSON object merged verbatim into every
request to that endpoint — the provider's
[`extra_body`](providers.md#extra_body-connection-scoped-fields). It is how
reasoning gets configured, in the endpoint's own dialect, since one
OpenAI-compatible flavor hides many: `{"reasoning_effort": "medium"}` for
OpenAI, DeepSeek, Qwen 3.8 and vLLM; `{"chat_template_kwargs":
{"enable_thinking": false}}` for a vLLM/SGLang-served Qwen or GLM;
`{"thinking": {"type": "disabled"}}` for the DeepSeek and Z.ai APIs;
`{"output_config": {"effort": "medium"}}` for Anthropic. A `null` value removes
a field the adapter would send (`{"stream_options": null}` for gateways that
reject it). Nothing is translated, and the endpoint decides what it accepts —
an official API rejects unknown fields on the next message, while vLLM may
ignore them silently; the thinking trace in the chat is the real check. The
chips under the editor insert these shapes; they claim nothing about the model.

Every call through the profile carries the fields — the main reply, background
subagents, compaction summaries, and, for a profile assigned the vision or aux
role, image descriptions or titles and follow-ups. A second parameter set on the
same endpoint is a second profile: **Duplicate** copies one server-side (key
included, since the page never holds it), so "thinking off for titles" is a
copy with `enable_thinking: false` assigned to the aux role. Thinking models
need output room: set **Max output tokens** under the profile's advanced
fields (a `max_tokens` in the extra fields wins over it). It caps the chat
replies and background subagents that run on the profile, not titles,
follow-ups or image descriptions, and the context policy's output reserve
does not follow it.

The search backend, Tavily key, and skill directories also live in
`config.json`. Model connections, additional profiles, role assignments,
search, and skills have no CLI flags; use Settings or maintain the file
directly.

## Skills

Skills load from two places; each skill is a subdirectory with a `SKILL.md`
(see [Skills](skills.md)):

- **The project's own** `.agents/skills`, in the root of the directory a chat
  works in. It is a convention, like `AGENTS.md`, not a setting: it loads in
  every chat there, and wins on a duplicate skill name. A chat on a remote
  workspace does not get its project's skills yet.
- **The global directories**, managed in Settings → Skills — an ordered list
  of absolute or `~` paths, the same for every chat; on duplicate names the
  earlier one wins. It defaults to `~/.agents/skills`; remove it to disable
  it, or add any other directory. A relative entry is refused, since it
  would name a different directory in every chat (an old `.agents/skills`
  entry in a saved file is dropped: it loads anyway).

The pane lists only the global directories, with what each contributes —
discovered skills, parse problems, shadowed duplicates — and a directory that
does not exist is simply skipped, so a stale entry never prevents the server
from starting. Changing the list applies immediately; adding a skill inside
an already-listed directory needs no action at all. An agent may load its own
list of directories instead (below); the project's own skills load either way.

Type `/` at the very start of the composer and it lists the skills this chat
can use, filtered as you type; ↑/↓ move, Enter or Tab inserts `/name `, Esc
closes. The list is exactly what the chat's run loads — the project's skills,
duplicate-name precedence and the agent's skill settings included; a remote
chat lists only the global directories'. The message goes out as typed, and
the model loads the skill by the [`/name` rule](skills.md#calling-a-skill-by-name).
A `/` later in the message or on a later line opens nothing, since the model
only honours the one that starts it.

## Agents

`lovia web` serves one agent, `lovia`, until you add more in **Settings →
Agents**. Each agent is a way of working: its own name, prompt, model,
default permission mode and auto-allowed commands, switches (web search,
[MCP tools](#mcp-servers), memory, background subagents, follow-ups) and, under Advanced, skill
directories and run limits. Models, keys and the search service are configured
once (Settings → Models, Settings → Web search) and shared; an agent refers to them.
The agent menu shows each agent's model and permission mode under its name.

- A new chat picks its agent on the chip beside the folder and mode chips,
  shown when there is more than one. It starts with the agent the folder's
  latest chat used, else the default — the first in the list (**Make
  default** moves one there) — and in that agent's permission mode.
- A chat keeps the agent it was created with; the chip only names it. A chat
  whose agent is deleted carries on with the default agent, and the page
  says so when it opens. A schedule whose agent is deleted fails, naming it,
  until it is pointed at another.
- An agent's `id` is what chats and schedules store. It is derived from the
  name when the agent is created and never changes, so renaming keeps every
  chat.
- Memory is shared by every agent that keeps it, and split by project (see
  [Memory scopes](memory.md#scopes)): notes about you are one list, carried
  into chats in every project; notes about a project, and recall over its
  past chats, are kept per chat directory. A chat in "No project" has only
  the first. **Settings → Memory** edits either scope, "About me" or a
  project picked from the list — starting at the open chat's folder; a
  sidebar folder's **⋯ → Project memory** opens it on that folder. **Clear project memory** there deletes that
  project's notes and recall for good; deleting chats never clears memory. A project is its
  directory, so renaming or moving it starts a fresh one. Notes saved by
  earlier versions all stay under "About me"; move project facts by hand.
  Recall over chats from earlier versions stays at the top level, searched
  only from "No project" chats.

In `config.json` a missing field takes its default ([below](#settings-that-apply-without-a-restart)):

```json
{
  "version": 2,
  "agents": [
    {"id": "lovia"},
    {"id": "review", "name": "Code review",
     "instructions": "Review the change; don't edit files.",
     "mode": "readonly", "model": "fast"}
  ]
}
```

!!! note "Upgrading from 0.9.179 or earlier"

    `config.json` version 1 kept the one agent's settings in its `agent`,
    `run` and `workspace` sections. Those are not converted: the default agent
    starts from the defaults (its prompt, permission mode, auto-allowed
    commands, switches and limits), and the next save writes version 2.
    Models, roles, search and skills carry over.

## MCP servers

**Settings → MCP** lists the [MCP](mcp.md) servers whose tools the agents
use — every agent with its **MCP tools** switch on (the default). Add servers
by pasting the `mcpServers` JSON other clients use; several at once is fine:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
      "env": { "SOME_TOKEN": "…" }
    },
    "docs": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer …" }
    }
  }
}
```

A server is a `command` run on the lovia web host (stdio) — for a remote
chat too — or a `url` (streamable HTTP; SSE is not supported). Its name
prefixes its tools (`docs__search`), so it is letters, digits, `-` and `_`.
`"disabled": true` saves it switched off; other keys are ignored.

- **Connected per message.** Each run connects to the servers as it starts,
  one after another, and closes them when it ends — an `npx` server makes
  every message start a little later. Nothing stays running between runs.
- **Every tool call asks first**, except in **Full access**: View only and
  Edit in workspace govern files and the shell, not what an MCP tool does to an
  outside system, while a Full access shell already reaches those systems
  unasked (`curl`, `gh`), so a prompt there would protect nothing. The card
  offers **Always allow in this chat** for that tool, as for a command.
- **No replays.** A call whose connection drops fails; the server may still
  have done it, and the error says so instead of trying again.
- **A server that can't be reached is skipped.** It has 30 seconds to start
  and list its tools; past that, or on any error, the run goes on without
  it, the chat shows which server did not connect and why, and the model is
  told.
- **Each row shows the last connection** — from a run or the **Test**
  button: its outcome, when, and the tools it listed. Testing runs the
  server's command now; saving tests the servers just saved. This is kept
  in memory, so a restart starts every row at "not connected yet".
- **Secrets stay on the server.** `env` and `headers` values come back
  masked (`ghp…a1b2`). In the editor, a masked value left as it is keeps
  what is saved, an emptied one is removed, a new one replaces it; a
  masked value that matches nothing saved is refused. `command`, `args`
  and `url` are shown as written, so a token belongs in `env` or `headers`,
  not in an argument or the URL's query.

MCP support comes with the `web` extra. Servers are saved in `config.json` as `mcp`.
Background subagent tasks don't get MCP tools: no one is there to approve
their calls.

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

`--app MODULE:ATTR` accepts one Agent, a `{name: agent}` mapping, or an app
built with `create_app()` (or a zero-argument factory returning any of them).
An app is served as built — your own auth, storage, and UI options — with the
CLI still handling host, port, logging, and the startup banner; options it
would otherwise pass to `create_app()` (`--token`, `--db`, `--title`, …) are
ignored with a warning. For Python deployment and ASGI integration, see
[Web server](web-server.md).

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

A Markdown image whose path is workspace-relative — `![chart](uploads/chart.png)`
— displays the Workspace file itself, in a reply and in the Files panel's
Markdown preview alike. Paths resolve against the document's own directory and
then the Workspace root, and are served read-only through the same endpoint as
the preview. SVG renders too, through the endpoint's attachment form: browsers
ignore `Content-Disposition` for an image, where an SVG's scripts are disabled,
and honor it for navigation, where they would run — so the picture shows and
the URL only ever downloads.

A link — `[report](report.md)` — points at the same endpoint. In a reply it
opens in a new tab when the browser can display the file (images, PDF) and
downloads it otherwise; either way the conversation stays put. Inside the Files
panel the same link opens in the viewer instead. External links in a reply also
open in a new tab.

In an empty composer, ↑ brings back this chat's earlier messages, newest
first, to send again or edit; ↓ goes the other way, back to empty.

To point the Agent at a file without attaching it, type `@` at the start of the
message or after a space: the composer lists the chat folder's files, matched
as you type — file names first, then folders, then the letters in order. ↑/↓
move, Enter or Tab inserts `@path `, Esc closes. A path with a space goes in
quoted, `@"docs/my notes.md"`; a path holding a double quote, a line break or
another control character is not offered (type it out instead). Only the path
is sent, as typed — the model opens the file itself with `read_file`, so nothing
changes about permissions or context size.

The list is what the Agent's own `list_files` sees: in a Git repository it
follows the repository's ignore rules, dotfiles are left out (as in the Files
panel), and so is anything the Workspace's `denied_paths` hide; a remote chat
lists the host's files. The browser keeps one chat's list and fetches it again
after each run, so files the Agent made show up. The list stops at about 20,000
entries; past that the menu says how many files it has, and the rest can be
typed by path.

Vision configuration controls whether images are sent inline:

- Official OpenAI and Anthropic endpoints are treated as vision-capable by
  default. For another multimodal endpoint, set the profile's vision
  capability to supported (`"vision": "on"` in `config.json`).
- A text-only main model can assign the vision role to another profile. The
  main model then receives text through the `describe_image` Tool; raw image
  data does not enter its conversation history.

Uploads default to 25 MiB per file (`LOVIA_MAX_UPLOAD_MB`). Named extensions
must match the built-in allowlist; override it with
`LOVIA_UPLOAD_ALLOWED_EXT`, separated by commas or spaces, or use `*` for any.

The Files panel opens on **Changes**: the files this chat wrote, plus the
ones attached to its messages or uploaded from the panel, newest first. In a
git work tree a folded second group lists the other uncommitted changes
`git status` sees under the folder, and each row carries its line counts
(`+12 −3`) or a *new* mark. A changed tracked file opens on its diff against
the last commit, with a *File* tab beside it; a new file opens as itself.
Outside git there is only the chat's own group, and no diff. Git runs on the
host that holds the files, limited to `git rev-parse`, `git status` and
`git diff`, and a path the agent could not read unasked never shows.

**Browse** is the folder tree: a folder opens in place, a folder holding
nothing but another folder shows the chain as one row (`src/app/core`), and an
open folder's row stays at the top while its contents scroll. The arrow keys
move through it. An open file's "⋯" menu shows it in the tree. A folder too
large for the workspace's listing cap (`max_list_results`) lists that many
entries and says so.

A file uploaded from the Files panel is not automatically attached to the next
message. The change list leaves out the agent's scratch dir `tmp/`, and the
"new files" badge counts only writes the list would show; that is
presentation only — browsing shows every directory, and any path the agent
references (an inline image under `tmp/`, a tool card's *open in Files*)
opens as long as the agent itself could read it. Deleting a chat does not
remove files under `uploads/`; the application or user must clean them up.

Background processes belong to the chat rather than one Run. Finishing a Run
does not stop them; deleting the chat or stopping the Web server reaps them,
and a server restart does not restore them. See
[Workspace](workspace.md#background-processes) for the full lifecycle.

## Charts

The `show_chart` tool draws interactive charts in the chat — line, bar, pie,
scatter, heatmap, candlestick, sankey and the other
[Apache ECharts](https://echarts.apache.org/) series types. The model writes an
ECharts option as the call's argument, and the card renders it, live and when
the chat is reopened. The default agent of `lovia web` has the tool; for your
own agent, add it: `tools=[show_chart]` (from `lovia.web`).
Any tool can produce a chart too: `return ui_result("chart", option)`, which
draws from real data without the model copying numbers (see
[UI components](web-server.md#ui-components)). A tool that takes the option
from the model — say one that runs a query and puts the rows into the option's
`dataset` — checks it with `validate_chart_option(option)` first: it raises the
same tool errors as `show_chart`, so the model can fix the option and retry.

The chart's title is shown as the card's heading, above the chart, so it
never collides with the legend. A toolbar beside it (shown on hover)
downloads the chart as PNG or SVG (title included), downloads its data as CSV
(JSON for trees, sankeys and graphs, which have no table shape), and opens the
chart full screen.

Good to know:

- ECharts (about 1 MB, bundled) loads with the first chart, so chats without
  charts never fetch it.
- The chat's HTML export includes each chart as an SVG image.
- The option is sanitized before it is drawn. Images, links, the `graphic`
  and `toolbox` components, and HTML tooltips are removed, and so are series
  types that need code or map data (`custom`, `map`).
- For house conventions — preferred chart types, units, titles — write them
  into the instructions or a Skill. No code is needed.
- The palette and inks are CSS variables (`--lv-chart-1` … `--lv-chart-8`,
  `--lv-chart-text`, `--lv-chart-title`, `--lv-chart-axis`, `--lv-chart-grid`). Override
  them in the `head` block of
  [your own template](web-server.md#customizing-the-page) to match a brand.

## Tables

The `show_table` tool shows the user an interactive table instead of a
Markdown one: click a heading to sort (again to reverse, a third time to
restore the order), type to filter longer tables, and page through 20 rows at
a time. Numbers align right and sort as numbers. A toolbar copies the table
(tab-separated, ready to paste into a spreadsheet), downloads it as CSV, or
opens it full screen; both exports take the current view, filtered and sorted.
The chat's HTML export includes it as a plain table with every row.

The default agent of `lovia web` has the tool; for your own agent, add it:
`tools=[show_table]` (from `lovia.web`). The
more useful path is often a tool of your own that returns
`ui_result("table", {"title": ..., "columns": [...], "rows": [[...], ...]})`:
the rows come from your data — a query, an API — and the model never writes
the numbers out, which is faster, cheaper, and exact. Cells are strings,
numbers, booleans, or null, and values are shown as given. Keys the table
doesn't know are ignored, so the same props can carry extras meant for the
model, such as a total row count or the SQL that produced them. For a large
result, keep the rows out of the model's context with `put_ui_data`: the
model reads a preview, and the table shows every row (see
[Bulk data](web-server.md#bulk-data)).

## Pages

The `show_html` tool shows an interactive page inline: a calculator, a
simulation, a custom visualisation, anything a chart or table can't do. The
model writes a self-contained page, with inline CSS and JavaScript, and the
card runs it in a sandbox. The default agent of `lovia web` has the tool; for
your own agent, add it: `tools=[show_html]` (from `lovia.web`). A tool of your
own can return `ui_result("html", {"html": page, "title": ...})`.

What the page can and can't do:

- It can't reach the app or the network. It runs in an opaque origin (no
  cookies, storage, or access to the chat), and its policy blocks every
  request: no `fetch`, CDN scripts, web fonts, or remote images. Links don't
  navigate.
- It matches the chat's look through CSS variables: `var(--lv-ink)`,
  `var(--lv-surface)`, `var(--lv-border)`, `var(--lv-accent)`, `var(--lv-chart-1)` …
  `var(--lv-chart-8)`, in light and dark themes.
- A button can ask a follow-up as the user: `lovia.send("Break it down by
  region")`. This works only from a real click inside the page.
- The card grows with the page, up to most of the screen, and has full screen
  and an HTML download.
- When you reopen a chat, its pages wait for a click on **Run**, so a page
  stuck in a loop can't freeze the chat each time it opens. A theme switch
  reloads a page.
- The chat's HTML export has no scripts, so it shows the page's source
  instead.

## Settings that apply without a restart

Everything about the agents lives in `config.json` in the home and is
edited in Settings. A save applies from the next run; a reply already streaming
finishes with what it started with.

| Settings pane | `config.json` | Default |
| --- | --- | --- |
| **Models** → a model's advanced fields | `models[].max_tokens` | The provider's |
| **Agents** → Model | `agents[].model` | `null`: the default chat model |
| **Agents** → Custom instructions | `agents[].instructions` | Empty: the chat's [project instructions](#project-instructions), else a generic prompt; set, they are appended |
| **Agents** → Default permission mode | `agents[].mode` | `coding` — see [modes](workspace.md#modes) and [below](#permission-mode-per-chat) |
| **Agents** → Commands that run without asking (shown for Edit in workspace) | `agents[].auto_allow` | `read-only`, `dev-tools`: the [command bundles](workspace.md#modes) coding mode runs without asking (`ls`, `git status`, …; `pytest`, `ruff`, …); an empty list asks for every command no rule allows |
| **Agents** → Advanced → Skills | `agents[].skills` | `null`: the global directories; a list replaces them |
| **Agents** → Web search | `agents[].search` | On; off leaves out `web_search` (`web_fetch` stays) |
| **Agents** → MCP tools | `agents[].mcp` | On: the servers turned on in [Settings → MCP](#mcp-servers) (`mcp[]`) |
| **Agents** → Long-term memory | `agents[].memory` | On, kept in `<home>/memory` |
| **Agents** → Background tasks | `agents[].subagents` | On |
| **Agents** → Suggest follow-up questions | `agents[].followups` | On |
| **Agents** → Advanced → Max turns / Retries | `agents[].max_turns` / `agents[].max_retries` | `100` / the core default (`4`) |

When `config.json` isn't writable (a read-only mount, say), the server still
starts and Settings shows these as view-only.

## Workspaces

Each chat works in a folder of its own. The sidebar groups chats by folder:
the one `lovia web` was launched in first, then the rest by recent activity,
with **No project** (`<home>/workspace`, for chats that belong to no
project) always listed. A folder's **+** starts a new chat there; its
**⋯** removes it from the list — files and chats stay, and adding the folder
again brings its chats back. The folder button beside **Chats** adds
one, completing the path as you type; the view button next to it switches
between a group per folder and one list by date, and between the chats in
use and the archived ones. Drag the sidebar's right edge to resize it
(double-click resets).

**Archive** in a chat's **⋯** menu puts it away without deleting anything:
it leaves the list (a search still finds it) until you unarchive it, pin
it, or send it a message.

A new chat starts in the folder this tab last used — the folder chip in the
composer shows which, with the full path on hover; pick another there before
sending. A chat keeps its folder for good: to work elsewhere, start a new
chat. Attachments are saved into the draft's folder, so the chip locks while
the draft holds some. The Files panel shows the open chat's folder, and the
agent reads that folder's [`AGENTS.md`](#project-instructions) and
`.agents/skills`.

**A folder on another machine.** Unless the server was started with
`--lock-workspace` (or `--no-workspace`), the add dialog has three tabs:
**Local**, **SSH** and **Container**. On the last two, name the host — an alias
from `~/.ssh/config` or `user@host`; a running container, or
`user@container` to be someone other than its default user — and the list
shows *its* folders. Chats in that folder run there: the agent's file and
shell tools work on the host, under the chat's permission mode, and the
sidebar marks the folder with a server icon. The host needs `python3` 3.10
or newer and nothing installed, and SSH must work without a password prompt
(keys or an agent). The Files panel shows the host's folder, and uploads
and attachments are saved there (under `uploads/`, as in a local one); a
large file takes as long as the link to the host does. The agent reads the
folder's [`AGENTS.md` files](#project-instructions) on the host, by the same
rules, once per run. The folder's own skills are
not loaded yet: only the global skill directories apply. If the
connection drops, the run in progress fails and says so; the next message
reconnects. See [Remote workspaces](workspace.md#remote-workspaces).

Chats from before folders existed gather under **Not linked**. Reading them
needs nothing; the first message to one asks which folder it works in
(folders where its approvals were given come first), and **Link…** on the
group links several at once. A link can't be changed later.

A server started with `--lock-workspace` keeps every chat in its launch
folder: there is nothing to pick, and the sidebar lists chats by date as
before.

## Permission mode per chat

Each chat has its own permission mode — **View only**, **Edit in
workspace** or **Full access** (`readonly`, `coding` and `trusted` in
`config.json` and the API; see [modes](workspace.md#modes)) — on the chip
beside the folder
chip in the composer. A new chat starts in its agent's mode; pick another
before sending to start it there. Switching a chat applies from its next run:
a reply already streaming finishes in the mode it began, and the chat's
background processes (a dev server, say) keep running. The chats a run
creates — background subagent tasks, chats started with `start_session`,
the fresh chats of a schedule set up from it — take the mode that run used.

The chip offers what the server allows (see the limits below). A chat set to
a mode the server no longer offers — it was restarted with `--readonly`, say
— runs in the widest offered mode below it, and the chip says so; it is never
run wider.

## CLI options

The flags are the server's own, plus the workspace limits a deployment sets.
Each resolves as flag, environment variable, then default.

| Flag | Environment | Default |
| --- | --- | --- |
| `DIR` (positional) | `LOVIA_WORKSPACE` | `.`: where new chats work — or `<home>/workspace`, the general workspace, when launched from your home or the root directory |
| `--home` | — | `~/.lovia`: config, chat history, memory and the general workspace |
| `--readonly` | — | Off. On, every run is read-only and Settings can't change it |
| `--no-workspace` | — | Off. On, no file or shell tools |
| `--lock-workspace` | — | Off. On, every chat works in `DIR`: the browser can't open another, on this machine or a remote one |
| `--host` / `--port` | `LOVIA_HOST` / `LOVIA_PORT` | `127.0.0.1` / `8000` |
| `--root-path` | `LOVIA_ROOT_PATH` | None; the prefix a proxy serves the app under ([details](web-server.md#serving-under-a-path-prefix)) |
| `--token` | `LOVIA_WEB_TOKEN` | Not needed on loopback; generated otherwise |
| `--db` | `LOVIA_DB` | `<home>/lovia.db` |
| `--app MODULE:ATTR` | `LOVIA_APP` | Build the default Agent (or: an Agent, mapping, or app of your own) |
| `--title` | `LOVIA_TITLE` | `lovia` |
| `--log-level` | `LOVIA_LOG_LEVEL` | `info` |
| `--check` | — | Check model configuration and exit |

The bind address decides only whether a token is required: a server on
`0.0.0.0` (in a container, say) offers every mode, folder and remote
workspace that one on `127.0.0.1` does. A deployment narrows it with the
three flags above. Before 0.9.179 a non-loopback `--host` capped modes at
`coding` and kept chats in `DIR` unless `--allow-workspace-switch` was given;
that flag is gone.

The options that moved into Settings in 0.9.146 (`--trusted`,
`--auto-allow`, `--max-turns`, `--max-retries`, `--max-tokens`,
`--no-followups`, `--no-memory`, `--no-subagents`, `--instructions`,
`--instructions-file`, and their `LOVIA_*` variables) stop the CLI with a
message naming the setting that replaced them; `--memory-dir` names
`--home`. A custom `--app`
Agent runs with the CLI defaults; set its limits with `create_app(max_turns=…,
retry=…, followups=…)` in code. Use `lovia web --help` for the complete list.

## Approvals

A Tool whose [permission](tools.md#tool-approval) verdict is `ask` pauses
the run on a card that asks in the Tool's own words ("Run command?"), adds
the verdict's *reason* underneath when something stands out ("Pushes commits
to a remote."), shows the call as the model made it, and offers **Approve**
and **Deny** — with an optional, multi-line reason for denying that the model
sees, so it can change course instead of retrying (⌘/Ctrl+Enter denies with
it). The card takes keyboard focus on arrival, but not its Approve button: a
stray Enter never approves anything.

When the verdict names *scopes* (`shell:git push`, `read:~/notes`), the card
also offers **Always allow in this chat** for exactly those — a chained
command such as `echo a; uname -a | head -1` lists each command it would
remember (`echo` `uname` `head`), and none at all when one of its parts cannot
be granted (`sudo`, a `$VAR` the shell expands). The checkbox picks them all;
click a chip to leave it out (or, before ticking anything, to pick just that
one) — so `pwd` and `uname` can be remembered without `python`. The call
itself still runs whole; the pick only decides what later calls skip asking.
The
grants are kept on the server, per chat, and tied to the agent and workspace
root they were given under: a later call they fully cover is approved before
anyone sees it, with no card; a chat rebound to another
workspace does not reuse them. Grants never apply to a `deny`, and nothing is
inherited by new chats or subagent tasks.

The card is there only while the decision is open. Once decided — by you,
another tab, the timeout or a stop — it goes, and the call's own tool row
carries the outcome as a badge: **Approved** (hover: what was remembered),
**Denied** (hover: your reason), **Timed out**, **Stopped**, or
**Auto-approved** for a call grants answered — click it to list the chat's
grants and revoke one (revoking affects later calls only). A refused call
gets its row at once, filled by its error result, as a reloaded chat shows
it. `lovia web` denies an approval nobody
answers within ten minutes (the card says when, up front; the model is told
the user did not answer), so a scheduled or background run never holds a run
slot indefinitely; `create_app(approval_timeout=...)` sets it for your own
app (`None`: wait for an answer).

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
from lovia.web import create_app, serve

channel = HumanChannel()
agent = Agent(name="bot", model="<model>", tools=[ask_human(channel)])
serve(create_app(agent, question_channel=channel, question_timeout=600))
```

## Follow-up suggestions

Follow-up suggestions use a separate model call and never enter the Transcript.
Turn them off for an agent in **Settings → Agents** to avoid that extra call, or point them at a
cheaper model by assigning it the aux role ([Models and
switching](#models-and-switching)). They are opt-in for a custom Agent served
through `create_app()`; see [Web server](web-server.md#follow-up-suggestions).

## Run and connection boundaries

Runs are managed by the server. Refreshing or closing the page does not cancel
a Run, and reconnecting resumes from server state. Explicitly stopping a Run
still preserves completed Turns in the Session.

A graceful server shutdown retains the main Run's Checkpoint so it can resume
after restart. Process-local approval and SSE subscription state is not
restored, nor are Shell background processes or background subagents.

A message sent while a Run is active does one of two things, decided per message:

- **Queue** (Enter): the message waits above the composer and is sent as its own
  Run once the active Run has *completed*. Several queued messages go one at a
  time, each after the previous one's Run completes. A Run that fails or is
  stopped pauses the queue; **Send now** or withdrawing the message moves on.
  Queued messages are held by the browser tab: they go only while their chat
  is open there, and a reload drops them.
- **Steer** (⌥/Alt+Enter, or **Steer** on a queued message): the message joins
  the Run at its next Turn. If the Run finishes first, one sent with
  ⌥/Alt+Enter starts the next Run in order; a queued message steered too late
  returns to the head of the queue. A steer that gets no answer (e.g. the
  network dropped) may have landed, so it is never resent on its own: it waits
  at the head of the queue, paused, for you to check the chat.

Attachments can be added while a Run is active: they go with the message,
queued or steered. Send waits for any upload still in progress. A queued
message can also go back to the composer for editing — text and attachments,
when the composer and its tray are empty — or be withdrawn.

## Scheduled runs

**Scheduled runs** in the sidebar opens a page of the schedules
(`?view=lovia-schedules`): active, paused, and ended (folded away), each with
its name, when it runs next, its last outcome, and a pause switch. A schedule
opens its own page with the instruction, its settings, and the run history —
each run links to its chat; a fire skipped because the previous run was still
going is listed too, with why.

**New schedule** (or one of the examples on an empty page) opens a form: a
name, the instruction, and when — **Once**, **Daily** (weekdays and a time),
**Interval**, or a raw **Cron** expression — with the next three times shown
before you save. Where a chat may pick a folder and permission mode, so may a
schedule; its fires run unattended, so anything that needs approval is denied
after the approval timeout. A stop condition and limits sit under the fold.
Cron times follow the server's clock; the form says so when yours differs.

A chat can also set one up: ask "every weekday at 9, summarize…" and approve
the `schedule_run` call. Its runs then continue that chat (unless you ask for
a new chat each time), which shows its schedules in the top bar. A chat a schedule's run created carries a clock in the sidebar and links
back to its schedule.

## Branching a chat

**Branch in new chat**, on any finished reply, starts a new chat with
everything up to and including that reply, so you can take the conversation in
another direction from there. Branching from the last reply copies the whole
chat. A reply followed by a Run still in progress gets the action once that Run
has finished. A branch copies what the server holds at that moment. The original
is left untouched, the new chat links back to it, and deleting one leaves the
other.

A branch copies the conversation, not the workspace: files written since the
branch point stay as they are now.

Ask the agent to do something "in a new chat" and it can start one itself
(`start_session`). Its card opens the new chat, which starts with none of
this conversation and links back to the chat that started it.

## Background subagents

`lovia web` enables [background subagents](multi-agent.md#background-subagents)
by default; turn them off for an agent in **Settings → Agents**. A default child keeps Skills,
Todo, Tools, and Workspace, but omits Scheduling, Memory, and Subagents, so it
cannot spawn recursively.

A custom Agent must attach the `Subagents` Plugin. `create_app()` wires a
Plugin using its default execution mode into Web supervision; disable that with
`create_app(..., wire_subagents=False)`. An app mounting `build_api_router`
directly must call `wire_subagents(deps)` once.

Each background task has an independent Session. When it completes, its report
enters the parent's next Turn if the parent Run is active; otherwise the server
starts a Run to process the report. The browser does not need to remain online.

A task is not owned by the parent Run, so stopping the parent does not stop the
task; deleting the parent chat stops its tasks and deletes them with it. Approvals auto-deny after `approval_timeout`; unfinished tasks are not
resumed after a server restart.

## See also

- [Web server](web-server.md) — Python API, lifecycle, and scheduling
- [HTTP API](http-api.md) — build a different front end
- [Tools: approval](tools.md#tool-approval) — approval flow and fail-closed behavior
- Example: [`26_web_serve.py`](../../examples/26_web_serve.py)
