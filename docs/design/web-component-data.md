# Web components: static export, and bulk data outside the model's context

> Status: implemented (v0.9.81: static export, v0.9.82: component data) · 2026-10-14 · Scope: `lovia/web` only (core untouched)
>
> Line numbers refer to v0.9.80 (`3cd3bb3`).

## Summary

Two follow-ups to the UI components work
([web-ui-components.md](web-ui-components.md), [web-tables.md](web-tables.md)):

| PR | What | Code | Public API |
| --- | --- | --- | --- |
| 1 | **Static export (G3).** The HTML export draws `table` and `chart` components (a static `<table>`, a chart image) instead of printing their envelopes as raw JSON. | ~150 JS + ~15 CSS, no Python | **Breaking (JS):** `registerRenderer(kind, { render, snapshot? })`; `getRenderer` returns that object |
| 2 | **Component data (#231).** A tool keeps bulk data out of the model's context: the envelope carries a preview, the full props live in the chat store, and the page fetches them. | ~120 Python + ~50 JS | Additive: `put_ui_data()`, `ui_result(..., data=)`, one endpoint, one optional envelope key |

Envelopes and the Python API don't change shape. The one break is PR 1's
registry value (see [The seam](#the-seam-snapshot)), taken while lovia web
is its only consumer. Neither PR touches core.

PR 1 comes first. It is independent and smaller, and PR 2's export support
reuses its async export path.

## PR 1: static export (G3)

### Current state

The HTML export is built client-side ([export.js](../../lovia/web/static/js/export.js)).
`exportSessionHtml` fetches the JSON export and `buildExportDoc` turns each
message into HTML. A tool result goes into a collapsed `<pre>`
([export.js:51](../../lovia/web/static/js/export.js#L51)). A chart or table
therefore exports as an envelope: `{"lovia_ui": {"kind": "chart", "from_args": "option"}}`.
For the `from_args` form that is not even the data, just a pointer to the
call's arguments. Mermaid fences stay as code for the same reason: the export
has no runtime.

### Design

1. **Detect components.** `buildExportDoc` already maps call id → tool name.
   It also maps call id → arguments. For each non-error tool result it calls
   `parseEnvelope(output, args)`, the same function the live view uses
   ([components.js:54](../../lovia/web/static/js/components.js#L54)). This
   handles both the `props` and `from_args` forms with no new parsing code.
2. **Snapshot it.** If the kind has a snapshot function, the component
   replaces the raw `<pre>` with the snapshot's HTML, drawn visibly as part
   of the answer. The call's arguments stay in their existing collapsed
   "Tool: …" block, so nothing is lost. A kind without a snapshot, or a
   snapshot that throws, falls back to today's `<pre>`.
3. **`buildExportDoc` becomes async.** Snapshots load ECharts and, after
   PR 2, fetch data. The function is module-internal; only tests import it.

### The seam: `snapshot`

```js
lovia.registerRenderer('table', { render: renderTable, snapshot: tableSnapshot });

/** @typedef {(props: unknown, ctx: SnapshotContext) => string | Promise<string>} Snapshot */
// SnapshotContext = RenderContext minus `send` (an export can't speak for the user).
```

- **The registry value becomes an object.** A first draft kept
  `registerRenderer(kind, render)` and added an optional third argument. That
  needs a second map, plus a `getSnapshot` beside `getRenderer`, and a
  wrapper then has to fetch and re-register two things. With `{ render,
  snapshot }` there is one map, one accessor, and one unit to wrap:
  `getRenderer('chart')` returns both. The API is two days old and lovia web
  is its only consumer, so the break is taken now rather than designed
  around. A function is not accepted as well: there are no dual signatures
  before 1.0.
- Registering a kind replaces the whole object. A user who swaps in their
  own `table` without a snapshot therefore gets the raw-JSON fallback, not
  the built-in snapshot drawing props it may not understand. If the props are
  the same, they can keep it: `snapshot: lovia.getRenderer('table')?.snapshot`.
- `render` is called as a method, so a renderer object may use `this`.
- The built-ins register through the same call. This keeps the rule from the
  components design: if built-ins can be built on the public seam, so can a
  user's components.
- The snapshot returns an HTML string. The export passes it through
  `DOMPurify.sanitize` like every other piece of HTML in the file
  ([export.js:40](../../lovia/web/static/js/export.js#L40)). The export is a
  standalone file opened anywhere, so the sanitizer is the trust boundary,
  not the snapshot's author.

### Table snapshot (`tables.js`, ~30 lines)

- `normalizeTable(props)`, `numericColumns` and `cellText` are already
  exported and unit-tested. The snapshot builds an escaped `<table>` from
  them: numeric columns right-aligned, the title as a `<figcaption>`.
- No script. The export carries no JS today, and a sort widget would be the
  first. A static table is enough for an archive.
- All rows are included (the live table caps at 64 KiB through
  `show_table`; with PR 2, at the data cap). `EXPORT_CSS` gets
  `overflow-x: auto` and the numeric alignment, ~10 lines.

Every row is exported, uncapped: an export is an archive, and silent
truncation loses data.

### Chart snapshot (`charts.js`, ~25 lines)

- `loadEcharts()` loads it lazily on the first chart, as live charts do.
- ECharts server-side rendering also works in the browser, with no DOM
  element:

  ```js
  const chart = ec.init(null, themeFromCss(), { renderer: 'svg', ssr: true, width: 720, height: 360 });
  chart.setOption({ ...option, animation: false });
  const svg = chart.renderToSVGString();
  chart.dispose();
  ```

  `sanitizeOption` and `liftTitle` run first, exactly as for the live chart.
  The heading becomes a `<figcaption>`, matching the live card.
- It is embedded as `<img src="data:image/svg+xml;charset=utf-8,<percent-encoded SVG>" alt="<title, else 'Chart'>">`,
  not as inline SVG. An image cannot run script or pick up the page's CSS,
  so the snapshot needs no SVG-specific sanitizer tuning, and DOMPurify
  allows `data:` on `<img>` by default.
- Colours come from `themeFromCss()`, which reads the live page. The export
  already bakes in the theme it was exported from (`data-theme`), so chart
  and page match.
- The width of 720 px matches the export's content column (760 px − padding).
  The image scales down with `max-width: 100%`.

### Optional, same mechanism: mermaid

`diagrams.js` already caches source → SVG. A `language-mermaid` block in the
export could become the same kind of `<img>` (~15 lines). It is not a
component, so it doesn't go through `snapshot`. It is listed here because a
reader of the export will hit both gaps together.

### Tests and size

- Node:
  - the table snapshot (escaping, alignment, title);
  - `buildExportDoc` with a stubbed snapshot (props form, `from_args` form,
    unknown kind → `<pre>`, a throwing snapshot → `<pre>`);
  - registration replacing both functions.
- `/verify`: export a session with a chart and a table, open the file, check
  both draw and that the file loads nothing over the network.
- Size: ~150 JS, ~15 CSS, ~90 tests.

## PR 2: component data (#231)

### Why lovia-level now

The user-side pattern from the issue is built and working in
`lovia-db-agent`: `results.py` (a 131-line SQLite cache with TTL and LRU) and
`static/db.js` (69 lines of `db.table` / `db.chart` wrappers). What it
cannot do from user space:

1. **Die with the chat.** The cache can't see a chat being deleted, so a
   deleted conversation's query results stay on disk until the TTL expires.
   That is a privacy bug, not a tidiness one. Only the chat store knows when
   a chat dies ([store.py:498](../../lovia/web/store.py#L498)).
2. **Export.** PR 1's export sees `db.table` and has no idea where its data
   lives.
3. **Ownership.** The user-side endpoint checks only an unguessable id. A
   session-scoped endpoint gets the same guard as every other session route,
   which is what the multi-user work builds on.
4. **No glue per tool.** Today every data tool (SQL, logs, HTTP) repeats the
   cache, router, wrapper kind and auth wiring.

After PR 2 the DB agent deletes `ResultCache`, its router, `db.js` and the
TTL/size settings. Its tools return plain `table` / `chart`. That is about
−190 lines there for about +170 lines here.

### The model: `data` is the full props, `props` is the preview

The issue asks whether a preview/stats shape can be generic across kinds.
This design avoids needing one:

- The stored data is **the complete props of the same kind**: a full
  `{"columns", "rows"}` for a table, a full ECharts option for a chart.
- The envelope's `props` is **the preview**: whatever the tool wants the
  model to read *and* the page to fall back to. That might be the head rows
  plus a row count and column stats, or a downsampled option plus a summary.

Renderers already ignore keys they don't know (the table ignores extras; the
chart sanitizer keeps only allowlisted keys), so model-facing extras like
`stats` or `sql` ride along harmlessly. **No renderer changes.** The
registry hands a renderer either the full props or the preview, and both are
valid props of its kind.

### Envelope

```json
{"lovia_ui": {"kind": "table", "props": {"columns": [...], "rows": [/* 20 */], "row_count": 10000, "stats": {...}}, "data": "k3J9…"}}
```

- `data` is an optional ref (`[A-Za-z0-9_-]{16,64}`), valid only with the
  `props` form. The `from_args` form is for model-written data, which is
  never bulk.
- `props: null` with a ref means "no useful preview". If the data is gone,
  the page shows only the notice. This suits a chart whose preview would be
  an empty plot.
- An old page that ignores `data` shows the preview. A new page reading an
  old envelope sees no `data`. Both directions degrade cleanly.

### Python API

```python
from lovia.web import put_ui_data, ui_result

@tool
async def query_table(sql: str) -> str:
    columns, rows = await run_query(sql)
    if len(rows) <= 20:
        return ui_result("table", {"columns": columns, "rows": rows})
    ref = await put_ui_data({"columns": columns, "rows": rows})
    preview = {"columns": columns, "rows": rows[:20], "row_count": len(rows),
               "stats": column_stats(columns, rows)}
    return ui_result("table", preview, data=ref)
```

- `put_ui_data(value) -> str | None` stores the value in the chat store of
  the current session and returns a ref. Outside a `lovia web` run (CLI,
  evals, tests) it returns `None`, and `ui_result(..., data=None)` emits a
  plain envelope. The same tool therefore works everywhere and degrades to
  the preview. It is not an error: the tool did nothing wrong.
- **How a tool reaches the store** (the issue's first open question): a
  `ContextVar` holding `(store, session_id)`, set wherever the web app starts
  a run. One place is the top of every supervised task, next to the existing
  `RUN_SOURCE` ([supervisor.py:489](../../lovia/web/supervisor.py#L489)),
  which covers chat, scheduled and subagent runs; each subagent task has its
  own session. The other is the non-streaming `POST /api/chat`.
  `create_task` copies the context, so tool tasks inherit it.
  Core stays untouched: the var lives in `lovia.web`, and only tools that
  opt in import it.
- The write completes **inside the tool, before it returns**. So the data
  exists before the `tool_result` event reaches any browser, with no race
  and no ordering coupling to the supervisor's event stream.
- Validation reuses `_check_json_size` (finite JSON) with a cap of
  `MAX_UI_DATA_BYTES = 8 MiB`. How that number is derived: the DB agent caps
  results at 10,000 rows, and a row of 10–20 typical columns is ~200–500 B
  of JSON, so 2–5 MB. 8 MiB leaves headroom. Past that, the client table
  (no virtual scrolling) gets sluggish anyway. An oversized value raises
  `ToolError` with a hint ("aggregate, or return fewer rows"), so the model
  can narrow the query.

### Store

One table in the chat store's metadata SQLite (`_META_SCHEMA`). It is a new
table, so `CREATE TABLE IF NOT EXISTS` adds it to existing databases with no
migration:

```sql
CREATE TABLE IF NOT EXISTS chat_ui_data (
    id TEXT PRIMARY KEY,          -- secrets.token_urlsafe(16)
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    payload BLOB NOT NULL         -- zlib-compressed JSON (tabular JSON shrinks ~5–10×)
);
CREATE INDEX IF NOT EXISTS idx_chat_ui_data_session ON chat_ui_data (session_id);
```

- `ChatStore.delete` and `delete_all` remove the session's rows alongside its
  transcript, checkpoint and runs.
- **Retention is the chat's lifetime. There is no TTL.** Transcripts don't
  expire in core, and a data-only TTL would be the kind of presumptuous
  serving-layer default lovia avoids. Rows written by a run that was later
  cancelled stay until the chat is deleted. That is harmless, and it is the
  same as an interrupted run's other side effects.
- In-memory stores (`meta_path=":memory:"`) behave the same and are lost on
  restart. The page then falls back to the preview, which is consistent with
  how such a store treats metadata.
- Per-user apps (lovia-multiuser) get one store per user, so isolation comes
  for free.

There is no size cap or pruning job for now (see *Deferred*).

### Endpoint

`GET /api/sessions/{session_id}/ui-data/{ref}` → the stored JSON.

- It sits on the sessions router, so it is behind the same `auth=` / token
  guard as every session route.
- It returns 404 unless the row's `session_id` matches the path, so a ref
  leaked from one chat is useless outside it.
- The content never changes for a given ref, so it is sent with
  `Cache-Control: private, max-age=31536000, immutable`.
- No paging. One fetch of ≤ 8 MiB matches what the table renderer pages
  through client-side. Server-side paging waits for virtual scrolling.

### Client (`components.js`, ~50 lines)

- `parseEnvelope` returns `{ kind, props, data? }`.
- `run(m)`: if `m.data` is set, fetch
  `${basePath}/api/sessions/${sessionId}/ui-data/${ref}` once per mount.
  The promise is kept on the `Mount`, so a theme re-render doesn't refetch,
  and it is garbage-collected with the mount (no global cache to bound).
  - On success, render with the fetched props.
  - On failure (404, network), show a `.tool-ui-notice` (new label
    `'partial'`: "The full data is no longer available; showing a preview."),
    then the preview below it (nothing more when the preview is `null`). The
    failed fetch is dropped, so the next render (a theme switch) retries.
- The existing `gen` counter already discards a fetch that resolves after a
  re-render or an unmount.
- Renderers are unchanged and never see the ref.
- Export (PR 1) resolves refs through the same fetch helper before calling
  the snapshot, so an exported table carries its full rows.

### Rejected alternatives

| Alternative | Why not |
| --- | --- |
| Carry the full data on `ToolResultEntry.raw` ([transcript.py:116](../../lovia/transcript.py#L116)), which providers never see | Core deliberately drops `raw` above 16K chars to bound run memory, checkpoints and session storage ([tool_calls.py:92](../../lovia/runtime/tool_calls.py#L92)). This would reverse that decision, and it would reach every checkpoint write. |
| Sync `ui_result(data=full)` that buffers in the ContextVar; the supervisor flushes before forwarding `tool_result` | One call is nicer, but it couples correctness to the supervisor's event ordering, and data is lost if the run dies between return and flush. The explicit `await` costs one line. |
| Per-kind merge (`rows` from data, the rest from props) | Every kind would need a merge rule. "Data = full props" needs none. |
| A data-only TTL, or a global LRU cache | See *Store*: a serving-layer retention policy core doesn't have. |
| A top-level `/api/ui-data/{ref}` | No ownership check beyond the ref being unguessable. |

### Tests and size

- Python:
  - `put_ui_data` with and without an active store, plus the size and
    non-finite errors;
  - `ui_result(data=)` validation (ref format; rejected with `from_args`);
  - store round-trip and the delete cascade;
  - the endpoint: hit, wrong session → 404, auth guard;
  - a supervised run where a scripted tool calls `put_ui_data` (the
    ContextVar reaches the tool task).
- Node: `parseEnvelope` with `data`; the fallback path; one fetch per mount
  across re-renders.
- `/verify`: a stub model calls a 5,000-row tool. Check that live and replay
  show all rows, that deleting the chat makes the endpoint 404, and that the
  export contains all rows.
- Size: ~120 Python, ~50 JS, ~170 tests; docs for `web-ui.md` (en/zh), with
  the README pair one line each.

## Decisions

1. `snapshot` is public, as part of the renderer object.
2. The export's static table includes every row.
3. Component data lives as long as its chat, with no size cap for now.
4. The Markdown export is unchanged. A GFM table would need a Python mirror
   of `parseEnvelope`, a second envelope parser and a new cross-language
   contract, for the less visual format.

## Deferred

- Mermaid diagrams in the HTML export (same `<img>` mechanism, not a
  component).
- Table and chart components in the Markdown export.
- Server-side paging and virtual scrolling for very large tables.
- A size cap or pruning job for component data.
