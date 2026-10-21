# Web components: `html`, a sandboxed page in the chat

> Status: implemented (v0.9.84) · 2026-10-14 · Scope: `lovia/web` only (core untouched)
>
> Line numbers refer to v0.9.82 (`b97c79d`). Browser behaviour was checked
> with Chromium 145 (Playwright); see [Browser facts](#browser-facts).
>
> **Revised (v0.9.105):** the chat page's `frame-src 'none'` is gone. Each
> untrusted frame now sits inside a proxy frame carrying that policy itself
> (`static/js/sandbox.js`), which frees the page for deployers' iframes. See
> [web-sandbox-frames.md](web-sandbox-frames.md); the sections below that
> mention the page header describe the original design.

## Summary

One more built-in kind, `html`. It draws a self-contained HTML page, with its
own scripts and styles, in a sandboxed iframe inside the tool card. This is
the one kind that covers the long tail: a calculator, a custom visualisation,
an interactive report. Without it, each of those needs a deployer-written
renderer (L3). With it, the model writes the page (L0, `show_html`), or a tool
returns one (L2, `ui_result("html", ...)`).

| Part | What | Size |
| --- | --- | --- |
| JS: `static/js/html.js` | Renderer: frame, CSP, bootstrap, messages, sizing; static snapshot | ~180 |
| Python: `lovia/web/html.py` | `show_html` tool; on in `lovia web`'s default agent | ~60 |
| Page header | `Content-Security-Policy: frame-src 'none'` on the chat page | ~5 |
| CSS, docs, tests | | ~20 CSS; ~200 tests |

No new public API beyond the tool and the kind. The renderer registers through
`registerRenderer` like `chart` and `table`. The component-data path
(`put_ui_data`) already covers big pages.

## Goals and non-goals

Goals:

- The model or a tool can show an interactive page inline, live and on replay.
- The page cannot touch the app: no cookies, no API, no other chats.
- The page cannot send its own data anywhere (best effort; see
  [Residual risks](#residual-risks)).
- A button in the page can post a follow-up message, but only on a real click.

Non-goals (v1):

- **Being an MCP Apps host.** The message names follow MCP Apps so that a
  later bridge can reuse them, but v1 has no handshake, no tool calls from
  the page, and no `ui://` resources. See [MCP Apps](#mcp-apps).
- **External resources** (CDN scripts, web fonts, remote images, `fetch`). A
  page must be self-contained. A domain allowlist is deferred.
- **State across re-renders.** A theme switch reloads the page, as it
  re-draws charts and tables ([web-tables.md](web-tables.md), G4).
- **Links out of the page** (MCP Apps' `ui/open-link`). Deferred.

## Threat model

The page's HTML is untrusted. A model writes it, possibly steered by text in
the data it read (a database row, a web page), or a tool or MCP server returns
it. The page holds only its own props, which is what the model or tool put in
it.

| Guarantee | How | Strength |
| --- | --- | --- |
| **Isolation**: no access to the app | `sandbox="allow-scripts"` without `allow-same-origin`: an opaque origin. No cookies, no `localStorage`, no parent DOM, no same-origin API calls. Already relied on by the Files panel ([files.js:952](../../lovia/web/static/js/files.js#L952)). | Hard (the browser's origin model) |
| **No exfiltration** of the page's own data | A CSP in the frame blocks subresources and `fetch`. The `frame-src 'none'` of the document embedding the frame (originally the chat page, now a proxy frame, see the note above) blocks the frame from navigating itself away. The sandbox blocks popups, forms and top navigation. | Best effort: WebRTC remains ([Residual risks](#residual-risks)) |
| **No acting as the user** | A page's "send" needs both focus inside the frame (`document.activeElement === frame` on the chat page) and the existing `ctx.send` gate (`navigator.userActivation.isActive`). A click in the frame satisfies both; a script alone, or a keypress elsewhere on the chat page, does not. | Hard in Chromium (verified end to end); fails closed where the API is missing |

## Browser facts

Each was measured with a parent page, a sandboxed `srcdoc` frame, and a second
local server that logs every request reaching it.

| Question | Result |
| --- | --- |
| Frame with no CSP: what leaves? | `fetch`, `<img>`, and `location.href = …` all reach the logger. `window.open` is blocked by the sandbox. |
| Frame CSP `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:` | `fetch` and `<img>` are blocked. **Self-navigation still leaks**: CSP has no navigation directive. |
| Plus `frame-src 'self'` or `'none'` on the parent | Navigation is blocked, nothing reaches the logger, and the `srcdoc` frame still loads and runs under both. |
| The same policy on a proxy `srcdoc` between them, none on the page (v0.9.105; Chromium 145, WebKit 26) | Navigation is blocked the same way; messages relayed by the proxy, and the focus and activation checks on the proxy frame, behave as before. Details: [web-sandbox-frames.md §3](web-sandbox-frames.md#3-实测chromium-145webkit-26). |
| Click inside the frame, then the frame posts a message | The parent's `navigator.userActivation.isActive` is **true** in the message handler. A message posted from a timer with no click: **false**. Activation propagates to ancestors across the opaque origin. |
| …but the user just pressed Enter in the composer (end to end) | A page's timer that posts within ~5 s **passes** the activation check: activation is page-wide. Requiring `document.activeElement === frame` as well rejects it, while a click in the frame still passes. |
| A navigation blocked by `frame-src` | The frame's document is **replaced by an error page**, so an ordinary link click would destroy the page. The bootstrap cancels clicks on non-fragment links. |
| `RTCPeerConnection` inside the frame | Present. CSP does not govern it. |
| A 4 s busy loop in the frame | Default headless Chromium: the parent's timers stall **4 s** (same process). With `--site-per-process`: 0.1 s. Desktop Chrome isolates more than headless does, but other browsers and mobile need not. |

Method note: Playwright's `evaluate` runs with a user gesture, so the
activation test made no `evaluate` call before reading its results.

## Design

### Props and tools

Props: `{"html": str, "title"?: str}`. The renderer ignores other keys, as
the table and chart renderers do.

- **`show_html(html, title=None)`**: a presentational tool, `from_args=True`,
  in `lovia web`'s default agent (parent only), like `show_chart` and
  `show_table`. It checks that `html` is non-empty and at most
  `MAX_HTML_BYTES = 128 KiB`. How that is derived: a model's output limit is
  typically 16–32K tokens, about 64–128 KB of HTML, so 128 KiB rejects only
  pages a model could not have finished writing anyway. The docstring tells
  the model:
  - the page must be self-contained (no network, no CDN, no storage);
  - for a plain chart or table, use `show_chart` / `show_table`;
  - `lovia.send(text)` posts a follow-up from a button;
  - the design tokens are available as CSS variables.
- **A tool that builds a page** returns `ui_result("html", {"html": page,
  "title": ...})`. If the page is big, it stores the page with
  `put_ui_data({"html": page, ...})` and gives the model only a title and a
  description. That needs no new code.

### The frame

```
iframe  sandbox="allow-scripts"  referrerpolicy="no-referrer"
        srcdoc = PREFIX + props.html
```

`PREFIX` is the first thing the parser sees, so our policy and bootstrap come
before any of the page's own code:

```html
<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';
  script-src 'unsafe-inline'; style-src 'unsafe-inline';
  img-src data: blob:; font-src data:; media-src data: blob:; form-action 'none'">
<style>:root { --bg: …; --surface: …; --ink: …; --accent: …; --chart-1: … }</style>
<script>/* bootstrap */</script>
</head><body>
```

- **CSP.** This is MCP Apps' default policy with `'self'` dropped, since an
  opaque origin has none. A CSP only accumulates, so a `<meta>` the page adds
  later can tighten it, never loosen it. A page that is a full document
  (`<html><head>…`) still works: the parser folds its extra `html`/`head`/`body`
  tags into ours.
- **Tokens.** The `:root` block copies the chat page's computed design and
  chart tokens, so a page can match the UI with `var(--surface)`.
  `data-theme` is set too.
- **Bootstrap** (~30 lines, inline):
  - It reports the document's height on load and on every resize
    (`ResizeObserver` on `document.documentElement`).
  - It defines `window.lovia.send(text)`.
  - It cancels clicks on links other than `#fragment` ones, since a blocked
    navigation would swap the page for an error page.
  - It posts to the parent. Nothing on the parent's side trusts the
    bootstrap: the page can call `postMessage` itself, and every message is
    validated by the parent.
- **No `allow-forms`, `allow-modals`, `allow-popups`, or same-origin.** So
  `alert()` is a no-op, forms do not submit, and `localStorage` throws. The
  docstring says so.

### The chat page's `frame-src 'none'`

`build_ui_router`'s index response ([ui.py:157](../../lovia/web/ui.py#L157))
gains a `Content-Security-Policy: frame-src 'none'` header. lovia's only frames
are `srcdoc` (this one, and the Files panel's), which the policy allows, so
nothing of lovia's breaks. As a side effect, the Files panel's preview of
agent-authored HTML can no longer leak by navigating itself away, though its
subresources stay open (see [Deferred](#deferred)). A deployer template that
embeds a real iframe (a video, a dashboard) would be blocked. See
[Decision 1](#decisions).

### Messages

The method names and payload shapes follow MCP Apps
([spec 2026-01-26](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx)):
JSON-RPC 2.0 notifications over `postMessage`.

| Method | Params | The parent… |
| --- | --- | --- |
| `ui/notifications/size-changed` | `{height}` | Sets the frame's height to `clamp(80px, height, 70vh)`. Taller pages scroll inside the frame; full screen lifts the cap. |
| `ui/message` | `{role: "user", content: {type: "text", text}}` | Calls `ctx.send(text)` with `text` trimmed. Text over 4,000 chars is dropped, not cut. The message is refused unless focus is in the frame. `ctx.send` itself refuses without a live user activation and once the card has left the page ([components.js](../../lovia/web/static/js/components.js)). |

- The parent listens on `window` and accepts a message only if
  `event.source === frame.contentWindow`. The origin is `"null"` and proves
  nothing.
- Unknown methods are ignored. The listener is removed in the renderer's
  cleanup.
- There is no handshake: everything the page needs is in its `srcdoc`.

### The card

The page sits in a `figure.js` card like the chart and table: the title as the
heading, and a toolbar with **Download HTML** and **Full screen**. The
download is the frame's own document, `<title>`.html, policy included, so
the page opened from disk still can't fetch or load anything remote.
`form-action` is in the policy for that copy, because `default-src` doesn't
cover it. A top-level page can still navigate itself, which no policy
prevents. Full screen lifts the height cap. A theme
switch re-renders, which reloads the page with the new tokens (non-goal
above).

### Export snapshot

The HTML export has no scripts, and its snapshots pass through DOMPurify,
which drops iframes. So the snapshot is a static card: the title, a line
saying "Interactive page — open the chat to use it.", and the page's source in
a collapsed `<details><pre>`. See [Decision 3](#decisions).

### Replay and runaway scripts

A page with an infinite loop can freeze the whole chat in a browser that keeps
the frame in the chat's process. Worse, it freezes the chat again **every time
the chat is reopened**, because replay renders it again. See
[Decision 2](#decisions). The recommendation:

- **Live** components run at once, as now.
- **Replayed** components render the card with a **Run** button in place of
  the frame. One click runs the page, and the frame mounts lazily, like charts.

This needs one new `RenderContext` field: `live: boolean`, true when the
component arrives on the live stream and false on history replay. `chat.js`
knows which path it is on when it mounts. The field is additive, and other
renderers may use it too.

### Config and wiring

- No config switch. The builder appends `show_html` beside `show_chart` /
  `show_table` (which lost their switches in v0.9.83), to the parent agent
  only. A first draft kept `"html": true` because the page runs model-written
  scripts. It was dropped because `lovia web` is mostly used by one person, and
  the page leaks no more than lovia's page already allows: a Markdown image
  without an `img-src` policy is an easier way out than WebRTC. `create_app`
  users still add the tool themselves.
- `main.js` imports `html.js`, which registers the kind, so any tool's
  `ui_result("html", …)` draws, as with the other kinds.

## MCP Apps

MCP Apps (the `io.modelcontextprotocol/ui` extension) is the standard way for
an MCP server to ship UI: a tool's `_meta.ui.resourceUri` names a `ui://`
resource with MIME type `text/html;profile=mcp-app`, and the host renders it in
a sandboxed iframe.

| | This design | What an MCP Apps bridge adds |
| --- | --- | --- |
| Sandbox | One `srcdoc` frame with an opaque origin | For web hosts, the spec requires an outer **sandbox proxy on a different origin**. lovia serves one origin, so a bridge needs a second origin or a waiver. |
| CSP | MCP Apps' default, minus `'self'` | The server's `_meta.ui.csp` (`connectDomains`, `resourceDomains`, …) |
| Messages | `size-changed`, `ui/message` | `ui/initialize` handshake, `tool-input` / `tool-result`, `tools/call` back to the server, `host-context-changed`, `ui/open-link`, … |
| Source | Props from a lovia tool | `resources/read` on the server, through lovia's MCP plugin, which today skips resources ([mcp.py:45](../../lovia/plugins/mcp.py#L45)) |

The bridge would be its own kind (`mcp-app`) on the same frame code. It waits
for a user asking for MCP UIs. The shared message names keep it additive.

## Residual risks

- **WebRTC.** `RTCPeerConnection` exists in the frame and no CSP directive
  covers it, so a page could leak its own data through ICE traffic to a server
  the attacker runs. Shadowing the constructor in the bootstrap is bypassable
  (an `about:blank` child frame brings fresh copies), so v1 documents this
  instead of pretending to close it. For comparison, lovia's page today has no
  `img-src` policy, so a model-written Markdown image already leaks (which is
  why the DB agent adds one). A deployer who needs a hard guarantee serves
  an agent of their own without `show_html` and returns no `html`
  components.
- **A convincing fake UI.** A page can draw a "sign in" form inside the card.
  It cannot send what is typed anywhere (above), but the user may not know
  that. The card's frame and heading mark it as tool output, and the
  docstring forbids collecting credentials. There is no technical control.
- **CPU and memory.** A busy page can hang a tab that shares its process. See
  [Replay and runaway scripts](#replay-and-runaway-scripts) for the replay
  case. A live hang is fixed by a reload, which then replays paused.

## Decisions

1. *(Superseded in v0.9.105 by a proxy frame per untrusted frame; see
   [web-sandbox-frames.md](web-sandbox-frames.md).)*
   **The chat page sends `frame-src 'none'`, always.** It closes the leak by
   navigation for the `html` kind and the Files panel, and breaks nothing of
   lovia's. A deployer template's own iframes are blocked; a
   `ChatUI(frame_src=…)` knob waits for someone who asks. Sending it only
   when `html` is enabled would not work, because any tool can return an
   `html` component.
2. **Replay is paused, with a Run button** (`ctx.live`). The rejected
   options: running on replay too, which risks an unopenable chat; and
   always click-to-run, which adds a click to every live page.
3. **The export carries a static card plus collapsed source**, which keeps
   the export script-free. A live page in the export would carry scripts,
   and DOMPurify would have to be told to allow `iframe[srcdoc]`.
4. **`lovia.send` ships in v1**, gated on focus as well as activation (see
   [Browser facts](#browser-facts)). Buttons that ask a follow-up are the
   main reason to build an interactive page.
5. **No config switch** (see [Config and wiring](#config-and-wiring)).

## Deferred

- External resources: a `"html": {"resource_domains": [...]}` allowlist
  (scripts, styles, fonts, images; never `connect-src`), or serving the
  vendored ECharts to pages.
- `ui/open-link` (with a confirmation), `host-context-changed` (theme without
  a reload).
- An MCP Apps bridge (`mcp-app` kind).
- The Files panel's HTML preview could take the same frame CSP. It would then
  stop loading CDN scripts, which `examples/29` relies on (Chart.js), so that
  is a separate decision.

## Tests

- Python: `show_html` validation (empty, oversize, title); the
  `frame-src 'none'` header; builder wiring (always on, parent only).
- Node: the `srcdoc` assembly (CSP first, tokens, bootstrap, the page
  appended verbatim); the message handler (source check, clamps, unknown
  methods, the `send` cap and gate, removal on cleanup); the snapshot
  (escaped source, no iframe).
- End to end (`/verify`, Playwright): a stub model calls `show_html` with a
  page that resizes, posts `lovia.send` from a button, and tries `fetch`,
  `<img>` and navigation against a logging server. Check the height follows
  the page, the click posts a message while a timer does not, the logger sees
  nothing, replay shows **Run**, and the export has the static card.
