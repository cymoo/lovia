# Web UI components: tool results that render as UI

> Status: implemented (v0.9.76: seam, v0.9.77: charts) · 2026-10-10 · Scope: `lovia/web` only (core untouched)
>
> Line numbers refer to v0.9.75 (`616fa7a`).

## Summary

Some answers read better as UI than as prose: a line chart, a sortable table, a
KPI strip. lovia follows the pattern in Anthropic's write-up on effective
commerce agents ("The UI components are tools"): **a UI component is a tool
call**. The model calls a tool with typed arguments, the server validates them,
and the client renders the result. There is no custom markup in the reply and
no component spec in the system prompt. History replays without re-parsing,
because the data is just tool calls and tool results in the transcript.

The work ships as two PRs:

| PR | What | Size |
| --- | --- | --- |
| 1 | **The seam.** `ui_result()` (Python) wraps a tool's output in a *UI envelope*. `window.lovia.registerRenderer()` (JS) maps an envelope `kind` to a render function. `ChatUI(scripts=...)` loads the extension modules. Includes an example interactive table. | ~140 lines of code + tests + docs |
| 2 | **Charts, built on the seam.** A `show_chart(option)` tool that passes an ECharts option through, a vendored, lazily loaded ECharts renderer for kind `chart`, and an opt-in switch for `lovia web`. | ~340 lines of code + tests + docs |

PR 2 is the first consumer of PR 1. If charts can be built purely on the public
seam, so can a user's own components.

## Goals and non-goals

Goals:

- A user can make any tool's result render as a custom component in the chat
  transcript, both live and on replay, without forking lovia.
- Simple things stay simple:
  - Extra chart types or a house style need only a prompt or a Skill (L0).
  - Brand colours need only CSS variables (L1).
  - A domain tool that produces a chart needs only Python (L2).
  - Writing JS (L3) is only for genuinely new components.
- The `lovia web` CLI gets charts behind a config switch, off by default.

Non-goals:

- Changes to core: no new `Tool` fields and no new event types.
- Changes to the SSE protocol or the history serialisation. Both already carry
  everything this design needs.
- Sandboxed iframe components (MCP Apps style). They are possible later as one
  more renderer behind the same registry; see [Future work](#future-work).
- Rich charts in the HTML export. The export keeps the raw JSON in v1, just as
  it keeps mermaid source as code today.

## Extension levels

| Level | The user writes | Gets |
| --- | --- | --- |
| L0 | A prompt or a Skill | Any ECharts chart type and house conventions (PR 2) |
| L1 | CSS (`--chart-*` variables in the `head` template block) | Brand palette and fonts (PR 2) |
| L2 | Python: `return ui_result("chart", option)` | Domain tools that emit charts from real data, so the model never transcribes numbers |
| L3 | JS: `lovia.registerRenderer("table", fn)` plus `ChatUI(scripts=[...])` | Any component: interactive tables, KPI boards, maps, another chart library, a replacement for the built-in chart renderer |

## Current state: what the code does and does not allow

Already possible without lovia changes:

- **Load extra JS, CSS and libraries.** Use the `head` and `body_end` blocks of
  `ChatUI(templates=...)` ([index.html:41](../../lovia/web/templates/index.html#L41),
  [:341](../../lovia/web/templates/index.html#L341)), and mount your own
  static directory on the app that `create_app` returns.
- **Static images.** A tool can return an `ImagePart`, and the card shows it
  ([chat.js:912](../../lovia/web/static/js/chat.js#L912)).
- **Interactive HTML reports.** The Files panel renders workspace HTML in an
  `allow-scripts` sandboxed iframe
  ([files.js:950](../../lovia/web/static/js/files.js#L950)), which is what
  `examples/29` relies on. This is not inline in the chat, it needs a
  workspace, and the model has to author the whole page.

Not possible today: rendering a tool result as a component *in the
transcript*. The only route is a MutationObserver that scrapes `.tool` cards.
That depends on private DOM, class names and render timing (streaming,
windowed history, session switches), so it breaks on any refactor.

Gaps found while mapping the code. Each must be handled by the design:

| # | Finding | Consequence / handling |
| --- | --- | --- |
| G1 | A tool card keeps only `toolName` and `toolPath` and deliberately drops the arguments, which can be megabytes for `write_file` ([chat.js:827](../../lovia/web/static/js/chat.js#L827)). | A result that renders *from the call's arguments* has nothing to read. Keep the raw arguments in a `WeakMap<node, string>`, capped at 256 KiB, written in `buildToolNode` (used both live and on replay). |
| G2 | `setToolResult` is the single renderer for both live results and replay ([chat.js:954](../../lovia/web/static/js/chat.js#L954)), and it can run twice for one card: empty first, then the real result after a resume. | Hook component mounting into `setToolResult`. Mounting must be idempotent: unmount the previous component first. |
| G3 | The transcript is wiped in several places: `store.toolNodes.clear()` at [chat.js:672](../../lovia/web/static/js/chat.js#L672), [:1146](../../lovia/web/static/js/chat.js#L1146), [:1918](../../lovia/web/static/js/chat.js#L1918) and [:2078](../../lovia/web/static/js/chat.js#L2078), plus `innerHTML = ''`. | Hooking every call site is brittle. A single MutationObserver on `#transcript` sweeps mounts whose element has left the DOM and runs their cleanup. |
| G4 | Switching theme emits no event. `applyResolved` only sets `data-theme` ([ui.js:48](../../lovia/web/static/js/ui.js#L48)). | Emit `store.emit('theme-changed', theme)` from `applyResolved`. The registry re-renders the mounted components. |
| G5 | Sending is fused to the composer: the submit handler reads `promptEl.value` and owns the queue-while-streaming branch ([chat.js:3595](../../lovia/web/static/js/chat.js#L3595)). | Extract `sendText(text)` (queue path when streaming, new turn otherwise) and keep the submit handler as a thin caller. `ctx.send` calls it without touching the user's draft or attachments. |
| G6 | `lovia web` lists every user-facing change in `SURFACE_NOTE`, the tool docstrings, and both READMEs. | Document the seam in `docs/{en,zh}/web-ui.md`, the README pair and `examples/README.md`. `show_chart`'s docstring is enough for the model. |
| G7 | An agent-level `tool_result_renderer` applies to every tool and could rewrite a string result ([base.py:358](../../lovia/tools/base.py#L358)). | A renderer that wraps or truncates strings breaks envelopes. Document this; no code change. |
| G8 | Any tool output can carry an envelope, including `http_request` returning a remote body or an MCP server's result. | This is spoofing only, not code execution. Props are always untrusted, and each renderer must treat them that way (see [Security](#security)). Detection runs only on non-error results. |
| G9 | `ask_human` and todo detect their tool *by name* ([chat.js:1042](../../lovia/web/static/js/chat.js#L1042)), which needs a learned-names workaround for renamed tools. | Components are detected by the **result envelope** instead. No name lists, and renamed or wrapped tools work. |
| G10 | Lazy-loading a vendored file needs the versioned static prefix (`/static/<token>/…`). The page only knows `base_path` today ([ui.py:126](../../lovia/web/ui.py#L126)). | PR 2 adds `static_base` to `app_config` and pins the SRI hash. |

## PR 1: the seam

### UI envelope

A tool opts in by returning an envelope string as its output. There are two
forms:

```json
{"lovia_ui": {"kind": "table", "props": {"columns": ["sku", "units"], "rows": [["A1", 3]]}}}
{"lovia_ui": {"kind": "chart", "from_args": "option"}}
```

- `props` holds the data inline. This is the L2 form: the tool computed the
  data, so it has to live in the transcript, and the model sees it too, which
  it needs in order to discuss the data.
- `from_args` says the props are the call's own arguments: `true` for the
  whole arguments object, or a string naming one top-level field. This is the
  article's *presentational tool* form. The model already wrote the data as
  arguments, so echoing it back as the result would double the tokens.
- Exactly one of `props` or `from_args` is present. `lovia_ui` is the only
  top-level key.
- The client checks a non-error result with a cheap prefix test
  (`{"lovia_ui"`) before running `JSON.parse`.
- `kind` must match `^[a-z][a-z0-9_.-]{0,63}$`.

Python helper, in `lovia/web/components.py` and re-exported from `lovia.web`:

```python
def ui_result(kind: str, props: Any = None, *, from_args: bool | str = False) -> str: ...
```

It validates `kind`, enforces exactly one of `props` / `from_args`, and
`json.dumps(..., ensure_ascii=False)`. Non-JSON-serialisable props raise a
`UserError` with a hint. There is no size cap: the transcript cost is the
user's call. The docs recommend the reference pattern for bulk data (below).

### Renderer registry (JS)

New module `static/js/components.js`. It never touches the DOM at import time,
so it can be unit-tested in Node. It installs:

```ts
window.lovia.registerRenderer(kind: string, render: Renderer): void

type Renderer = (el: HTMLElement, props: unknown, ctx: RenderContext)
  => void | Cleanup | Promise<void | Cleanup>;
type Cleanup = () => void;

interface RenderContext {
  kind: string;
  theme: 'light' | 'dark';
  lang: 'en' | 'zh';
  basePath: string;              // mount prefix, for the user's own endpoints
  sessionId: string | null;
  callId: string | null;
  send(text: string): boolean;   // post a user message (see below)
}
```

Behaviour:

- **Mount.** `setToolResult` asks `components.js` to mount the component when
  the result is a non-error envelope. If the envelope is malformed or the
  `from_args` source is missing, the normal tool card renders as if there were
  no envelope, and a warning goes to the console.
- **Placement.** Same precedent as the image strip
  ([chat.js:928](../../lovia/web/static/js/chat.js#L928)): the component sits
  inside `.tool-result-box` as `<div class="tool-ui">`.
  - The card gets the `tool--ui` class and opens.
  - CSS hides `.tool-params` and the raw `<pre>`.
  - A *Source* action toggles them back for debugging.
  - Copy still copies the raw result.
  - Collapsing the card folds the component away.
- **Async renders.** Returning a Promise is allowed. If the mount is disposed
  before the Promise resolves, the resolved cleanup runs immediately.
- **Failure isolation.** A throw or rejection is caught. The component area
  shows "Couldn't render this component" and *Source* stays available. A
  broken renderer never breaks the transcript.
- **Unknown kind.** The area shows "No renderer for ‘x’". When `x` registers
  later, every pending mount of that kind upgrades in place, so script load
  order never matters.
- **Re-registering a kind replaces it** and re-renders that kind's mounts.
  This is deliberate: users can swap out the built-in `chart` renderer.
- **Theme change** (G4): every connected mount is cleaned up and rendered
  again. Renderers never subscribe to theme events themselves.
- **Disposal** (G3): a MutationObserver on `#transcript` (`childList`,
  `subtree`) checks the mount set only when a batch removed element nodes, and
  cleans up mounts whose element is no longer connected.
- **Sticky scroll.** On the live path, call `scrollDown()` after mounting,
  matching `appendTool`.

### `ctx.send(text)`

`ctx.send(text)` lets a component act on the user's behalf, for example "drill
into March". It goes through `sendText` (G5):

- While a run streams, the message is queued (the existing inject path).
- Otherwise it starts a new turn.
- The composer draft and pending attachments are untouched.

It requires **transient user activation**
(`navigator.userActivation?.isActive === true`; a browser without the API
fails closed), so a buggy or looping component can't send on render. It
returns `false` when the send is refused (no activation, empty text, no
session view).

### `ChatUI(scripts=...)`

```python
ChatUI(scripts=["ext/table.js", "https://cdn.example.com/x.js"])
```

- Each entry is emitted as `<script type="module" src="…">` right after
  `main.js` and before the `body_end` block. Module scripts run in document
  order, so `window.lovia` exists by the time they run. Late registration is
  handled anyway.
- Absolute URLs (with a scheme or starting with `//`) pass through unchanged.
  Anything else is app-relative and gets `base_path` prepended (a leading
  slash is optional). The same configuration therefore works at `/` and under
  a mount prefix.
- A bare string is treated as a single script, not as a sequence of
  characters, the same rule as `empty_examples`.
- lovia does not serve the files. The user mounts them, for example with
  `app.mount("/ext", StaticFiles(directory="ext"))`. The docs show the full
  snippet.

### Bulk data: the reference pattern

The envelope is the model-visible tool output, so `props` enter the context
window. For large data (a 10k-row table) the docs recommend:

1. The tool stores the rows server-side.
2. It returns `ui_result("table", {"summary": ..., "data_url": "/ext/rows/abc"})`.
3. The renderer `fetch`es `ctx.basePath + data_url`.

This works because components run in the page, on the same origin, so cookies
flow. The user's endpoint reuses the same `auth=` dependency they passed to
`create_app`.

### Security

- Renderer code is the deployer's own and is trusted. **Props are not**: they
  come from the model, tools, MCP servers or remote bodies (G8). Renderers must
  write text with `textContent`, or sanitise with the page's DOMPurify
  (`window.DOMPurify`). The docs say this prominently.
- lovia's own fallback and source views use `textContent` only.
- `from_args` reads only the call's own arguments, which are already shown on
  the card.

### Example

`examples/31_web_components.py` + `examples/web_components/table.js`:

- A `top_products(n)` tool returns `ui_result("table", ...)`.
- A vanilla-JS renderer (~100 lines) offers column sort, a text filter, and a
  per-row "Ask about this" button that calls `ctx.send(...)`.
- The app mounts the folder and passes `ChatUI(scripts=[...])`.

### Tests

- Python:
  - `ui_result`: both forms, validation errors, `ensure_ascii`.
  - `ChatUI.scripts` rendering: relative versus absolute URLs, under
    `root_path`, a bare string.
- Node:
  - Envelope parsing (both forms, malformed input, a missing field, a
    non-object argument, prefix-only matches).
  - The registry with a minimal fake element: late registration, replacement,
    errors, async cleanup after dispose.
- End to end (the `/verify` skill): a stub model calls a tool that returns an
  envelope. Check the live render, replay after reload, theme toggle, and
  `ctx.send`.

## PR 2: charts on the seam

### Why ECharts option pass-through

A lovia-specific chart schema would have to grow in lovia for every new chart
type. Passing the ECharts option through means:

- Every chart type ECharts has works with zero lovia code.
- Models already know ECharts option JSON well.
- There is no translation layer to maintain.

The cost is weaker schema enforcement. Mitigations: server-side checks give
the model a retry signal, and the client sanitises and falls back gracefully.

Size: `echarts.min.js` 6.1.0 (released 2026-05-19) is about 1.1 MB. It is
loaded only when the first chart mounts, so a session without charts pays
nothing. For scale, the page already loads mermaid at 2.7 MB.

### Server: `lovia/web/charts.py`

`show_chart(option: dict[str, Any]) -> str` is a `@tool` whose docstring
carries one compact example (`dataset.source` plus `series`, under ~150
tokens). The docstring also asks the model not to repeat the data as a
Markdown table.

Validation raises, so the call becomes a tool error and the model retries. It
checks that:

- `series` is a non-empty object or list, and every `series[].type` is in the
  allowlist;
- the serialised option is ≤ 64 KiB.

The series allowlist is: `line`, `bar`, `pie`, `scatter`, `effectScatter`,
`radar`, `tree`, `treemap`, `sunburst`, `boxplot`, `candlestick`, `heatmap`,
`parallel`, `funnel`, `gauge`, `sankey`, `graph`, `themeRiver`.

It excludes:

- `custom`, which needs a `renderItem` function that JSON cannot carry;
- `map` and geo `lines`, which need registered map data;
- `pictorialBar`, which needs image symbols.

On success it returns `ui_result("chart", from_args="option")`.

Wiring:

- Users serving their own agent use `from lovia.web import show_chart`.
- In `lovia web`, a `config.json` switch `"charts": true` (top-level
  `WebConfig.charts: bool = False`) makes `builder.py` add `show_chart` to the
  **parent** agent's tools only. A background subagent has nobody watching
  its transcript, the same reasoning as for `ask_human`.

### Client: `static/js/charts.js`

`charts.js` registers kind `chart` through the public `registerRenderer`, so
it uses the same API users get.

- **Loading.** `echarts.min.js` is injected on first use, with `integrity=`
  set and the URL taken from `app_config.static_base` (G10). The SRI value
  lives in `charts.js`, and `test_vendor_integrity.py` checks it against the
  vendored file and `vendor/README.md`. If the load fails, the area shows the
  failure notice plus *Source*.
- **Sanitising** happens client-side, because the client is the trust
  boundary: envelopes come from any tool, and replays include old data. The
  rules:
  - top-level keys are kept only if they are in an allowlist:
    `title legend grid xAxis yAxis polar radiusAxis angleAxis radar dataZoom
    visualMap tooltip axisPointer dataset series color textStyle aria
    calendar parallel parallelAxis singleAxis`;
  - `series[].type` is checked against the same allowlist as the server, and a
    test asserts the Python and JS lists match;
  - `link` / `sublink` keys are dropped (`window.open` could receive a
    `javascript:` URL);
  - string values starting with `image://` are dropped, as are
    `backgroundColor.image` objects (remote fetches usable as tracking
    pixels);
  - `tooltip.renderMode = 'richText'` is forced, because HTML-mode tooltip
    formatters render markup.
- **Rendering.** The SVG renderer, `aria.enabled`, and the ECharts locale set
  from `ctx.lang`. Height is a fixed responsive height
  (`clamp(240px, 45vw, 360px)`) at full card width.
- **Theme.** Colours are read from CSS variables at render time: `--chart-1`
  … `--chart-8` for the categorical palette, plus `--chart-text`,
  `--chart-title`, `--chart-axis` and `--chart-grid`. `styles.css` defines
  both light and dark sets. The palette was validated as an 8-slot
  categorical set (CVD-separated in order) against lovia's own surfaces.
  Users override the variables in the `head` template block. The registry's
  re-render on theme change does the rest. Slices and tiles part with a 2px
  line of the card surface.
- **Lifecycle.**
  - Initialise when the chart scrolls into view (IntersectionObserver), so
    replaying a session with dozens of charts stays cheap.
  - A ResizeObserver calls `chart.resize()`.
  - Cleanup disconnects both observers and calls `chart.dispose()`.
- **Errors.** If `setOption` throws, the area shows the error message plus
  *Source*.

### Tests

- Python:
  - `show_chart` validation (accepts a valid option; rejects an unknown or
    excluded type, an empty series, an oversize option);
  - builder wiring: off by default, parent only;
  - the vendor integrity entry;
  - the allowlist parity test.
- Node: the sanitiser (each rule above), and resolving the default palette
  from CSS variables (stubbed).
- End to end (`/verify`): a stub model calls `show_chart`. Check the live
  chart, replay, a theme toggle that recolours it, the lazy-load network
  request happening only once, and an invalid option showing the fallback.

### Docs

- `docs/{en,zh}/web-ui.md`: a "Charts" section covering L0–L2, the CSS
  variables, and the `config.json` switch.
- README pair: one line each.
- `SURFACE_NOTE` is unchanged: the tool describes itself.

## Size estimate

| Part | Code | Tests |
| --- | --- | --- |
| PR 1, Python: `ui_result`, `ChatUI.scripts` | ~40 | ~70 |
| PR 1, JS: registry, envelope, mount/dispose/theme, `sendText`, args WeakMap, CSS, types | ~150 | ~90 |
| PR 1, example (`table.js` + app) | ~150 | none |
| PR 2, Python: `show_chart`, config, builder | ~90 | ~80 |
| PR 2, JS: loader, sanitiser, theme, lifecycle, CSS | ~250 | ~80 |

## Risks

- **Model fluency with ECharts varies.** Strong models are fine; small local
  models may produce invalid options more often. Server checks plus the
  visible fallback keep failures legible. The user chose to skip a
  pre-implementation eval.
- **A public JS API is a commitment.** `registerRenderer` and `RenderContext`
  are kept deliberately small, and extra `ctx` fields can be added without
  breaking anything. Pre-1.0, changes follow the usual patch-bump policy.
- **Render failures are invisible to the model.** Rendering happens after the
  tool returned. We accept this. Server-side validation catches most problems,
  and a client-to-model feedback channel would be far heavier.

## Future work

- **Sandboxed iframe renderer.** One built-in renderer that hosts a
  user-provided HTML page in an opaque-origin iframe and talks to it over
  postMessage (props in; resize and send out). This gives framework freedom
  and hard isolation, and could align with MCP Apps so an MCP server's UI
  resources render in lovia.
- **Charts in the export.** Pre-render to SVG with ECharts SSR
  (`renderToSVGString`) while building the export document.
- **` ```chart ` fences in the Files panel's Markdown preview**, reusing the
  same renderer.
