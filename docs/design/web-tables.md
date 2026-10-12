# Web UI: built-in tables, and seams for data-backed components

> Status: implemented (v0.9.79: seams, #230; v0.9.80: tables) · 2026-10-13 ·
> Scope: `lovia/web` only (core untouched)
>
> Follows [web-ui-components.md](web-ui-components.md) (the UI envelope, the
> renderer registry, charts). Line numbers refer to v0.9.78 (`5f87588`).

## Summary

The UI envelope made "a tool result that renders as UI" possible, and charts
proved it end to end. Building a real data agent on it — a database Q&A agent
whose tools run SQL and answer with tables and charts — exposed what is still
missing:

| # | Gap | Fix | PR |
| --- | --- | --- | --- |
| 1 | An L2 tool that accepts a model-written ECharts option (say `query_chart(sql, option)`, which fills `dataset.source` from the query) must copy `show_chart`'s checks and import a private constant. | Public `validate_chart_option(option)`; `show_chart` calls it. | A |
| 2 | A renderer can't reuse a built-in one. To fetch bulk data and then draw it with the built-in chart, a user has to bundle ECharts and rewrite sanitizing, theming, the toolbar and export. | `window.lovia.getRenderer(kind)`, so a renderer can wrap another. | A |
| 3 | `create_app` owns FastAPI's `lifespan`, so an app's own resources (a DB pool) can't be opened and closed with it, short of patching `app.router.lifespan_context`. | `create_app(lifespan=...)`, nested outside lovia's own. | A |
| 4 | There is no table component. Tables are Markdown the model writes cell by cell: slow, costly, unsortable, and every number is a transcription. | A built-in `table` kind, plus a `show_table` tool. | B |

Tracked separately, not built here: keeping bulk data out of the model's
context while still rendering it (an issue links back to this doc; see
[Deferred](#deferred)).

## PR A: three small seams (v0.9.79)

### `validate_chart_option(option)`

The body of `show_chart` (`charts.py:70-97`) becomes a public function in
`lovia/web/charts.py`, re-exported from `lovia.web`:

```python
def validate_chart_option(option: Mapping[str, Any]) -> None:
    """Raise ToolError (with a hint) unless the chart UI can draw ``option``."""
```

It checks what it checks today — a non-empty `series` whose types are in
`SERIES_TYPES`, no NaN/Infinity, at most `MAX_OPTION_BYTES` serialized — and
raises `ToolError`, because its caller is a tool and a tool error is the
model's retry signal. `show_chart` becomes `validate_chart_option(option)` plus
its `ui_result` line. An L2 tool reads:

```python
@tool
async def query_chart(sql: str, option: dict[str, Any]) -> str:
    rows = await db.fetch(sql)
    option = {**option, "dataset": {"source": [rows.columns, *rows.values]}}
    validate_chart_option(option)
    return ui_result("chart", option)
```

Not exported: a `validate_table`. Chart options come from the model even inside
an L2 tool; table props are built by the tool from data, so there is nothing
untrusted to check before rendering (the client sanitizes anyway).

### `window.lovia.getRenderer(kind)`

```ts
window.lovia.getRenderer(kind: string): Renderer | undefined
```

It returns the renderer registered for `kind` right now. A renderer can then
wrap another, which is the pattern for data that lives behind the deployer's
own endpoint:

```js
const chart = lovia.getRenderer('chart');
lovia.registerRenderer('db.chart', async (el, props, ctx) => {
  const res = await fetch(`${ctx.basePath}/${props.data_url}`);
  if (!res.ok) { el.textContent = 'This chart has expired.'; return; }
  return chart(el, { ...props.option, dataset: { source: await res.json() } }, ctx);
});
```

Decisions:

- **Eager, not a lazy delegate.** A delegate that looks the kind up at call
  time would follow late registration, but it breaks the decorator case:
  re-registering `chart` with a wrapper around the old `chart` would find the
  wrapper itself and recurse forever. Eager has no such trap.
- **Load order is safe for built-ins.** `main.js` imports `charts.js` (and,
  after PR B, `tables.js`) before any `ChatUI(scripts=...)` module runs:
  module scripts execute in document order. For a kind registered by another
  user script, the deployer orders the scripts, as with any dependency.
- **The context passes through.** The wrapped renderer gets the wrapper's
  `ctx`, so `ctx.kind` is the outer kind. No built-in renderer reads it.
- **Wrappers re-run on a theme switch**, like every component. A wrapper that
  fetches should cache by URL; the docs example does.

Rejected: teaching the built-in renderers a `data_url` prop. It would bind lovia
to one storage convention — URL shape, auth, expiry, what an expired component
shows — that the wrapper leaves to the deployer, who owns the endpoint anyway.

### `create_app(lifespan=...)`

```python
Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[Any]]
# create_app(..., lifespan: Lifespan | None = None)
```

The callable has FastAPI's own `lifespan` shape. `create_app` enters it
**outside** lovia's (`RouterDeps.lifespan`):

```python
@asynccontextmanager
async def _lifespan(app):
    async with lifespan(app) as state, deps.lifespan(app):
        yield state
```

Outside, because lovia's shutdown winds down live runs, and their tools may
still be using the deployer's resources; those must close last. A yielded
state mapping passes through to `request.state`, as in plain FastAPI.

Rejected: `on_startup` / `on_shutdown` lists (Starlette deprecated them in
favor of `lifespan`), and documenting the `app.router.lifespan_context` patch
(it reaches into a Starlette internal).

## PR B: tables (v0.9.80)

### Why built in, when Markdown has tables

| | Markdown table | `show_table` (model writes the data) | L2 tool: `ui_result("table", ...)` |
| --- | --- | --- | --- |
| Who writes the numbers | The model | The model | **The tool, from the data source** |
| Output tokens | Every cell | Every cell | None |
| Sort, filter, page, CSV, full screen | No | Yes | Yes |
| Numbers right-aligned | No | Yes | Yes |

The renderer is what matters most: it lets a data tool answer with a table
whose numbers never pass through the model's output. `show_table` is the cheap
symmetric piece, like `show_chart` beside `ui_result("chart", ...)`.

### Props

```json
{"title": "Top stores", "columns": ["store", "revenue"], "rows": [["Xuhui", 1283400.5], ["Pudong", 990210]]}
```

- `columns`: strings. `rows`: arrays of scalars (string, number, boolean,
  null), one per column. A short row is padded with nulls; a non-scalar cell
  is shown as JSON.
- `title` is optional.
- **Unknown keys are ignored.** The props are also the model-visible output of
  an L2 tool, so the tool can carry model-facing extras in the same object —
  a row count, per-column stats, the SQL — without a second channel.
- No column types. A column whose non-null values are all numbers is numeric:
  right-aligned and sorted as numbers. Everything else sorts with
  `Intl.Collator(lang, {numeric: true})`, which also orders numeric strings
  (big-int ids sent as strings) and ISO dates correctly.

### `show_table`

`lovia/web/tables.py`, re-exported from `lovia.web`:

```python
@tool
def show_table(
    columns: Annotated[list[str], "Column headings."],
    rows: Annotated[list[list[str | int | float | bool | None]], "Rows, one value per column."],
    title: Annotated[str | None, "Optional heading."] = None,
) -> str:
    """Show the user an interactive table (sortable, filterable, CSV download).

    Use it for tabular answers longer than a few rows. The table is the answer —
    don't repeat it in Markdown; comment on what stands out.
    """
```

- The arguments already are the data, so it returns
  `ui_result("table", from_args=True)`: no echo, the same as `show_chart`.
- Pydantic validates the types. The tool also checks that `columns` is
  non-empty, each row has exactly `len(columns)` cells, there is no
  NaN/Infinity (which `json.loads` accepts and the browser rejects), and the
  serialized arguments are at most 64 KiB. Each failure is a `ToolError` with a
  hint. The NaN-and-size check is shared with `validate_chart_option` through
  one private helper in `components.py`.
- `lovia web`: `"tables": true` in `config.json` (`WebConfig.tables`, off by
  default like `charts`) adds it to the parent agent only — a background
  subagent's transcript has no one watching.

### Renderer: `static/js/tables.js`

Registered for kind `table` through the public `registerRenderer`, like
`charts.js`. What it draws:

- **Head**: the title and a toolbar (shown on hover, always on touch):
  *Copy* (tab-separated, pastes into spreadsheets), *CSV*, *Full screen*.
  Copy and CSV export the current view — filtered and sorted, every page —
  and both defuse cells a spreadsheet would run as a formula (a leading `'`).
- **Filter**: a text box, shown above tables of more than 10 rows. It matches
  any cell, case-insensitively, and reports `n of m rows`.
- **Sort**: header cells are buttons; a click cycles ascending, descending,
  original order. Nulls sort last either way. `aria-sort` marks the column.
- **Pages** of 20 rows (about a screenful inside a chat card, with no nested
  scroll area), with a pager when the view is longer. Paging also keeps the
  DOM small: a 10k-row table is 500 pages, not 10k `<tr>`s.
- **Cells** are written with `textContent` only. Values are shown as given:
  no locale grouping, because a "year" or an id column would read as
  `2,026`. A tool that wants `1,234.50` sends the string. Null is an empty cell.
  Long text wraps at word boundaries within a capped column width
  (`overflow-wrap: break-word`; `anywhere` would shrink min-content and break
  short words in narrow columns); a wide table scrolls sideways inside the card.
- **Full screen** shows the whole table area scrollable, toolbar visible.
- Labels in English and Chinese, from `ctx.lang`, as in `charts.js`.

### Shared figure card

`charts.js` already has the heading, the hover toolbar, the download helpers,
CSV escaping, and the full-screen toggle (`charts.js:229`, `:383-466`). Tables
need the same, so they move to `static/js/figure.js`:

- `figure(el, {kind, heading, lang})` → `{ fig, body, tool(label, face, action), filename(ext) }`
  builds the card, its toolbar and the full-screen button; `tool` reports
  failures with a toast.
- `toCsv`, `csvBlob` (UTF-8 BOM for spreadsheet apps), `save(blob, filename)`.

The CSS classes become `.figure`, `.figure-head`, `.figure-title`,
`.figure-tools`. They were internal (`docs/en/web-server.md`: class names may
change), and `charts.test.mjs` imports `toCsv` from its new home. Chart titles
keep `--chart-title`.

### Docs and the existing example

- `web-ui.md` (en/zh): a *Tables* section beside *Charts*.
- `web-server.md` (en/zh): built-in kinds are now `chart` and `table`; the
  custom-renderer example switches to another kind (a KPI strip) so it doesn't
  read as the way to get a table; a *Wrapping a renderer* subsection for
  `getRenderer`.
- `examples/31_web_components.py` keeps its own `table.js`: it now
  demonstrates **replacing** a built-in renderer (its version adds an "Ask"
  button). The docstring says so.
- The README pair: one line each.

## Gaps found while mapping

| # | Finding | Handling |
| --- | --- | --- |
| G1 | `Agent.max_tool_output_chars` (default 200K chars) truncates a long output head-plus-tail, which breaks an envelope's JSON; the card then shows raw text. | Document it next to `ui_result` (docstring and *UI components*): keep envelopes under the limit, which the 64 KiB checks already do for the built-ins. No code change: core can't know about envelopes. |
| G2 | `show_chart` and `show_table` both need "JSON-safe and ≤ 64 KiB". | One private helper in `components.py`. |
| G3 | The HTML export prints envelopes as raw JSON. | Unchanged. A table could export as a static `<table>`; deferred with chart export. |
| G4 | `charts.js` re-renders on theme switches and so would `tables.js`, which resets sort, filter and page. | Accepted, as for chart zoom. Keeping view state per element is easy to add if users notice. |

## Deferred

- **Bulk data outside the model's context.** Today an envelope *is* the
  model-visible output, so a 10k-row table costs 10k rows of context. The
  pattern that avoids it — preview rows in the envelope, full data behind the
  deployer's own endpoint, a wrapper renderer that fetches it — is buildable
  on PR A's `getRenderer` with no other lovia change. A lovia-provided result
  store waits for a second use case. Tracked in an issue.
- `show_table` options beyond the three arguments (column types, footnotes,
  frozen columns), virtual scrolling, and a `table_props()` value-conversion
  helper. Converting `Decimal`, datetimes and big integers is policy (lose
  precision or send strings?) that belongs to the tool, and `ui_result`
  already rejects what isn't JSON with a hint.

## Tests

- PR A — Python: `validate_chart_option` (valid, each failure);
  `create_app(lifespan=)` (enter and exit order relative to lovia's, state
  pass-through, absent). Node: `getRenderer` (registered, unknown, replaced
  after the call, wrapping its own kind without recursion).
- PR B — Python: `show_table` (valid, ragged rows, empty columns, NaN,
  oversize, `from_args` envelope); builder (off by default, parent only).
  Node: the pure helpers — row normalization, numeric-column detection,
  comparator (nulls last, numeric, collator), filter, paging, TSV/CSV of the
  view. End to end: a stub model calls `show_table`; check the live render,
  replay, sort, filter, paging, copy and CSV, and the theme switch.
