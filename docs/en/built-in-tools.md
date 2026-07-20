# Built-in tools

Nothing is wired into an agent automatically — every built-in is an explicit
import, so an agent's capabilities are visible at its construction site.

```python
from lovia import Agent
from lovia.tools.http import http_fetch
from lovia.tools.search import duckduckgo_search
from lovia.tools.time import now

agent = Agent(
    name="researcher",
    model="<model>",
    tools=[http_fetch, duckduckgo_search(), now],
)
```

File and Shell Tools come from [Workspace](workspace.md). The operator-input
Tool is covered in [Ask a human](#ask-a-human) below.

## HTTP fetch

`lovia.tools.http.http_fetch` — bounded, content-type-aware one-shot
requests.

| Argument | Default | Notes |
| --- | --- | --- |
| `url` | required | absolute `http://` / `https://` only |
| `method` | `"GET"` | any HTTP method |
| `headers` | `None` | optional request headers |
| `body` | `None` | sent as JSON for POST/PUT/PATCH |
| `timeout` | `30.0` | seconds, 1–120 |
| `max_chars` | `20_000` | result cap, 100–200,000 |

Responses are made model-friendly: JSON is re-serialized compactly, HTML is
reduced to its visible text, other text passes through, and binary returns
metadata only. Downloads stop at a hard 1 MB cap; the result is clipped to
`max_chars` with an explicit truncation notice, and every result starts with
a status header (`HTTP 200 · text/html · 3,214 chars`). Redirects are
followed; TLS honors the [`LOVIA_HTTP_*` settings](providers.md#networking-timeouts-proxies-tls).

> **No SSRF filtering.** The tool fetches whatever the host can reach —
> private and internal addresses included, and redirects may lead there.
> When the model is exposed to untrusted input, gate it
> (`dataclasses.replace(http_fetch, needs_approval=True)`) or isolate the
> network.

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

- **`http_fetch` is the sharpest built-in.** Combined with untrusted input
  it is an SSRF primitive — gate it or sandbox the network before exposing
  it in anything public-facing.
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
