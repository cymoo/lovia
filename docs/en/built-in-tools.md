# Built-in tools

Nothing is wired into an agent automatically — every built-in is an explicit
import, so an agent's capabilities are visible at its construction site.

```python
from lovia import Agent
from lovia.tools import duckduckgo_search, http_request, now, read_page

agent = Agent(
    name="researcher",
    model="<model>",
    tools=[read_page, http_request, duckduckgo_search(), now],
)
```

File and Shell Tools come from [Workspace](workspace.md). The operator-input
Tool is covered in [Ask a human](#ask-a-human) below.

## Reading web pages

`lovia.tools.read_page` reads a page *for its content*: the HTML becomes
Markdown, so headings, lists, fenced code, tables, links and images survive.
`[the guide](https://example.com/guide)` in the result is a real URL the model
can read next — which is exactly what flattening a page to plain text throws
away.

The model sees three arguments:

| Argument | Default | Notes |
| --- | --- | --- |
| `url` | required | absolute `http://` / `https://` only |
| `images` | `False` | also return every image the page references |
| `offset` | `0` | resume reading a long page at this character offset |

Everything else is an operator decision, so it lives on the `page_reader()`
factory rather than costing tokens in the schema on every call:

```python
from lovia.tools import HttpReader, page_reader

tool = page_reader(                     # or just use the ready-made read_page
    HttpReader(timeout=15.0, max_bytes=2_000_000, cache_ttl=60),
    max_chars=40_000,
)
```

### What the model gets back

```
Title: Example Domain
URL: https://example.com/guide
HTTP 200 · text/html

# Example Domain

This domain is for use in illustrative examples. See
[more information](https://www.iana.org/domains/example).

[... truncated. Continue with offset=20000.]

Images (2):
1. https://example.com/img/hero.png — Hero banner
2. https://example.com/social.png
```

The truncation notice carries the offset to continue from, so a long page is
never a dead end. `read_page` returns a `Page` dataclass (final URL, status,
title, Markdown text, images) — the block above is what its result renderer
produces for the model and the web UI.

### Images

Inline Markdown already carries every `<img src>`. `images=True` adds a
deduplicated, absolute-URL list that also covers the sources Markdown cannot
express: the largest `srcset` candidate, `<picture><source>`, and `og:image`.
Relative URLs resolve against `<base href>` and the post-redirect URL;
`data:`, `javascript:` and fragment targets are dropped — a single inline
base64 image can be 100 KB inside one attribute, and the model can do nothing
with it anyway.

### No JavaScript — swap the backend

`HttpReader` makes one plain HTTP request and parses with the standard
library. Nothing renders client-side, so a single-page app may come back
nearly empty. `PageReader` is the extension point (the same shape as the
`WebSearch` protocol):

```python
class PageReader(Protocol):
    async def read(self, url: str, *, images: bool = False) -> Page: ...
```

A hosted extraction service is a short class — here Jina Reader, which
returns Markdown directly:

```python
import httpx
from lovia.tools import Page, page_reader

class JinaReader:
    async def read(self, url: str, *, images: bool = False) -> Page:
        headers = {"X-Return-Format": "markdown"}
        if images:
            headers["X-With-Images-Summary"] = "true"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"https://r.jina.ai/{url}", headers=headers)
        return Page(
            url=url,
            status_code=resp.status_code,
            media_type="text/markdown",
            text=resp.text,
        )

agent = Agent(name="x", model="<model>", tools=[page_reader(JinaReader())])
```

Clipping to a character budget stays the tool's job, so a backend should
return the whole body.

### Caching and limits

Responses are cached by URL for `cache_ttl` seconds (default 300, bounded to
`cache_size` entries). That is mostly correctness: continuing with `offset`
would otherwise re-download the page and could splice two different versions
together. Downloads stop at a hard 1 MB cap — when that happens `size_capped`
is set and the notice says so, because no `offset` will ever reach the missing
tail. `4xx`/`5xx` bodies are capped at 500 characters; an error template is
not worth a full budget.

## HTTP requests

`lovia.tools.http_request` is an honest HTTP client for REST APIs and
non-HTML endpoints. It does not interpret HTML — that is `read_page`'s job.

| Argument | Default | Notes |
| --- | --- | --- |
| `url` | required | absolute `http://` / `https://` only |
| `method` | `"GET"` | any HTTP method |
| `headers` | `None` | optional request headers (override the default UA here) |
| `body` | `None` | sent as JSON for POST/PUT/PATCH |
| `timeout` | `30.0` | seconds, 1–120 |
| `max_chars` | `20_000` | result cap, 100–200,000 |

The result is a status line, the response headers, and the body: JSON
re-serialized compactly, text passed through, binary reduced to metadata.
Response headers are included because rate limits and `Link:` pagination are
usually the point — `set-cookie` is withheld, since it would drop a session
token into the transcript. Downloads stop at 1 MB and the body is clipped to
`max_chars` with an explicit notice. Redirects are followed and the final URL
is reported when it differs from the one requested. TLS honors the
[`LOVIA_HTTP_*` settings](providers.md#networking-timeouts-proxies-tls).

> **No SSRF filtering.** Both tools fetch whatever the host can reach —
> private and internal addresses included, and redirects may lead there. When
> the model is exposed to untrusted input, gate the tool or isolate the
> network.

To require approval only for requests that may change something, pass the
bundled predicate. It is not the default because approval fails closed: a
`Runner.run` caller with no approval handler would have every POST denied.

```python
import dataclasses
from lovia.tools import http_request, writes_need_approval

# GET / HEAD / OPTIONS run freely; everything else asks first.
gated = dataclasses.replace(http_request, needs_approval=writes_need_approval)
```

## Web search

`lovia.tools.search` — a pluggable search tool. Two backends are bundled:
DuckDuckGo (keyless, behind the `ddg` extra) and Tavily (no extra install —
set `TAVILY_API_KEY` or pass `api_key=`):

```bash
pip install "lovia[ddg]"   # only needed for the DuckDuckGo backend
```

```python
from lovia.tools.search import duckduckgo_search, tavily_search, web_search

tools = [duckduckgo_search()]            # keyless, requires lovia[ddg]
tools = [tavily_search()]                # Tavily API, reads TAVILY_API_KEY
tools = [web_search(MySearchBackend())]  # or your own
```

The tool (named `web_search` by default; override with `name=`) takes
`query`, `max_results` (1–20, default 5), and an optional `time_range`
recency filter (`"d"` / `"w"` / `"m"` / `"y"`). Results render as readable
title/URL/snippet blocks rather than raw JSON.

A custom backend implements one method — the `WebSearch` protocol:

```python
class WebSearch(Protocol):
    async def search(
        self, query: str, *, max_results: int = 5, time_range: str | None = None
    ) -> list[SearchResult]: ...
```

Backends must be safe for concurrent calls. Passing the backend explicitly
(`web_search(impl)`) means a missing optional dependency fails at
construction time, not mid-run.

## Time

`lovia.tools.time` — three small utilities:

- **`now`** (tool) — current wall-clock time as ISO-8601; optional `tz`
  takes an IANA name (`"Asia/Shanghai"`). Defaults to the server's local
  zone. (On Windows, IANA names need `pip install tzdata`.)
- **`sleep`** (tool) — sleep up to 60 seconds; for simple wait-then-check
  flows.
- **`current_date(tz=None)`** — *not a tool*: a factory returning an
  [instruction fragment](agents.md#instructions) that states today's date in
  the system prompt:

  ```python
  agent = Agent(name="researcher", model="<model>", tools=[duckduckgo_search()])
  agent.instruction(current_date())
  ```

  With the date in the prompt, the model writes the current year into
  searches instead of wasting a turn calling `now` first. It is date-only by
  design: a date is constant within any prompt-cache window, so it never
  meaningfully busts the [provider cache](providers.md#prompt-caching) —
  precise time, when needed, is `now`'s job.

## Ask a human

`lovia.tools.human.ask_human(channel)` — lets the *model* request operator
input mid-run (the inverse of approval, where the *runner* asks):

```python
from lovia import Agent
from lovia.tools.human import HumanChannel, ask_human

channel = HumanChannel()
agent = Agent(name="assistant", model="<model>", tools=[ask_human(channel)])

# elsewhere, the operator side:
async for q in channel.questions():   # ends when channel.close() is called
    channel.answer(q.id, "Use option A.")
```

The Tool call blocks until an answer arrives, the question is cancelled, or
the channel closes.

| API | Effect |
| --- | --- |
| `questions()` | Async-iterate queued questions; one consumer |
| `pending` | Snapshot of unanswered questions for polling |
| `answer(id, text)` | Resolve the question; the Tool returns `text` |
| `cancel(id, reason=...)` | Fail one question with a `ToolError` the model can see |
| `close(reason=...)` | Cancel outstanding questions, end iteration, and reject future asks |

Cancellation and closure become Tool-error results, so the model can continue
without the answer. Add a per-tool timeout when operators may be unavailable.
Calls from another thread must hop to the event-loop thread first, for example
with `loop.call_soon_threadsafe(channel.answer, qid, text)`.

Use approval when the question is “may I do this?” and expects yes/no. Use
`ask_human` when the model needs information only a person has and expects free
text.

## Sharp edges

- **`read_page` and `http_request` are the sharpest built-ins.** Combined
  with untrusted input either is an SSRF primitive — gate them or sandbox
  the network before exposing them in anything public-facing.
- **`read_page` caches responses for 5 minutes by default.** A page that
  changes faster than that needs `page_reader(HttpReader(cache_ttl=0))`.
- **`duckduckgo_search()` / `tavily_search()` construct eagerly.** They
  raise `UserError` at build time — missing `ddgs` package, missing
  `TAVILY_API_KEY` — a fail-fast you want at startup, not one to catch and
  ignore.
- **Search result quality is the backend's.** The DDG backend is keyless
  and rate-limited in practice; production apps usually use a keyed backend
  (`tavily_search()`) or their own `WebSearch`.

## See also

- [Tools](tools.md) — how these are built; write your own the same way
- [Workspace](workspace.md) — file and shell tools
- Examples: [`tools/01_http.py`](../../examples/tools/01_http.py),
  [`tools/02_time.py`](../../examples/tools/02_time.py),
  [`tools/03_search.py`](../../examples/tools/03_search.py),
  [`tools/04_human.py`](../../examples/tools/04_human.py)
