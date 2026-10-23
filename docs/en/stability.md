# Stability

What lovia 1.0 promises to keep working across releases, what it doesn't, and
which stored data a newer lovia still reads.

## The policy

- **Before 1.0**, breaking changes ship in patch releases.
- **From 1.0 on**, renaming or removing anything covered below keeps a
  deprecation shim for at least one minor release.

## Covered

**Python API** — the names in `__all__` of these modules:

| Module | Notes |
| --- | --- |
| `lovia` | Including `lovia.events`: the event classes a stream yields |
| `lovia.tools` | |
| `lovia.plugins` and `lovia.plugins.mcp` | |
| `lovia.workspace` | |
| `lovia.providers` | Plus the `lovia.providers` entry-point group for [third-party providers](providers.md#custom-providers) |
| `lovia.transcript` | The entry and delta types providers and plugins work with |
| `lovia.stores` | |
| `lovia.tracing` | |
| `lovia.testing` | |
| `lovia.eval` | |
| `lovia.context` | Except the Stage API (see below) |
| `lovia.web` | The Python API only |

**HTTP API** — the routes, request and response shapes, error codes and SSE
frames in [HTTP API](http-api.md), except `/api/config` and `/api/config/*`.

**The web page** — the public surfaces listed under
[Customizing the page](web-server.md#customizing-the-page): template blocks,
design tokens, `data-part` attributes, `data-i18n` keys and `window.lovia`.

**Built-in tool names** — `read_file`, `shell`, `todo_write`, `recall_tool_result`
and the rest. A name is stored in every transcript that called it, and tool
views, approval rules and evals match on it, so renaming one is a breaking
change.

## Not covered

These may change in any release:

- `lovia.runtime`.
- `lovia.context`'s Stage API: `Stage`, `StageContext`, the stage classes
  (`OffloadToolResults`, `ClearToolResults`, `SummarizeHistory`) and
  `Compaction(stages=...)`, `render_view`, and the prompt constants
  (`SUMMARY_SYSTEM_PROMPT`, `SUMMARY_WRAPPER`, `REQUIRED_SECTIONS`).
- `lovia.web.config` (the CLI's model configuration), `lovia.web.builder`, and
  anything in `lovia.web.api` beyond what `lovia.web` exports.
- Any name starting with `_`.
- The page's DOM structure, CSS class names, element ids and JavaScript
  modules, `api.js` among them.
- The `/api/config` and `/api/config/*` routes: they serve the CLI's built-in
  settings UI.
- Text written for the model or for people: built-in prompts, tool
  descriptions, compaction markers and log messages.

## Stored data

A newer lovia reads what an older one wrote:

| Data | Compatibility |
| --- | --- |
| `TranscriptEntry` JSON (`entry_to_dict` / `entry_from_dict`) | Unknown fields are ignored; an unknown `type` raises `ValueError` |
| SQLite stores (sessions, checkpoints, the web chat store, the memory indexes) | Each store's schema version is kept in a `lovia_schema` table and migrations are append-only. A database a newer lovia migrated is refused, not misread |
| `Segment.meta["context_state"]` | Carries its own `version`; state from another version is dropped and recomputed |
| The `lovia_ui` envelope in tool output | `{"lovia_ui": {"kind", "props" or "from_args", "data"?}}` (see [UI components](web-server.md#ui-components)) |
| Message refs | `"<run_id>:<n>"` |
| Built-in tool names | As above |
| `MEMORY.md` | One `- fact` per line; other lines are ignored, so people can edit it |
| `FileResultStore` | One `<key>.txt` file per stored value |
