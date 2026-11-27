"""Web search for lovia web: a hosted search MCP server, picked in Settings.

Each preset is one service's MCP endpoint, answering without an API key
within a free allowance (a key raises it). The ``web_search`` tool keeps one
shape — ``query``, ``max_results``, ``time_range`` — whichever service
answers, so the model, the prompt and the chat's view of the call never
change with the pick; each preset maps those onto its server's own tool.

``auto`` (the default) asks the services in :data:`AUTO_ORDER`, moving on
only from one that cannot be reached — a network that blocks one (a
filtering proxy, an exit IP a keyless allowance refuses) still searches.
An answer, a refusal included, is never asked of another service: that
would spend one free allowance after another.

A search connects when the model searches, not when a message starts: most
messages never search, and a connection costs about a second. Settings →
MCP can add any other search server; this one stays out of that list.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from ..exceptions import ToolError
from ..plugins.mcp import MCPToolResult, render_mcp_content
from ..tools import Tool, tool
from ..tools.fence import FENCE_NOTE, fence
from .config.schema import MCPServerConfig, SearchConfig
from .mcp import deadline, describe, server_for

# Per hit, and in all: hosted search answers run long (Parallel's excerpts
# reach 25k characters a call), and every one of them is the model's to read.
_HIT_CHARS = 1_500
_ALL_CHARS = 12_000
# ``time_range`` in words: Tavily's own, and the objective's.
_RANGE = {"d": "day", "w": "week", "m": "month", "y": "year"}
_RANGE_DAYS = {"d": 1, "w": 7, "m": 31, "y": 366}
# A page's own front matter, which Parallel's excerpts open with: the title
# and url once more.
_FRONT_MATTER = re.compile(r"\A---\n(?:[\w-]+:.*\n)+---\n*")
# One hit of Exa's plain-text answer; hits are divided by a ``---`` line.
_TEXT_HIT = re.compile(
    r"^Title: (.*)\nURL: (.*)\n(?:Published: (.*)\n)?(?:Author: .*\n)?(?:Highlights:\n)?",
    re.M,
)
AUTO_ORDER = ("parallel", "exa", "tavily")
# How long ``auto`` asks first the service that answered when the one
# before it could not be reached: a blocked service costs its failure once,
# not every search — and is tried again after a while (a laptop moves).
_REMEMBER = 600.0
# How long a service has to open a connection and list its tools: one that
# hangs (a network dropping its packets) must leave time for the next.
OPEN_TIMEOUT = 15.0
_clock = time.monotonic  # what ``_REMEMBER`` counts on
_NETWORK_HINT = (
    "A network problem, not a bad query; Settings → Web search shows whether "
    "the service answers."
)


def _objective(query: str, time_range: str | None) -> str:
    """``query`` as an objective, with the recency a service that has no
    filter for it (Parallel, Exa) can only be asked for in words."""
    if time_range not in _RANGE:
        return query
    return f"{query} (only pages published in the past {_RANGE[time_range]})"


@dataclass(frozen=True)
class SearchPreset:
    """One service: where its MCP server is, and how its search is called."""

    label: str
    url: str
    tool: str
    """The server's search tool, by its own name."""
    args: Callable[[str, int, str | None], dict[str, Any]]
    """``(query, max_results, time_range)`` → the tool's arguments."""
    key_headers: Callable[[str], dict[str, str]]
    keyless_headers: dict[str, str] = field(default_factory=dict)


PRESETS: dict[str, SearchPreset] = {
    "parallel": SearchPreset(
        label="Parallel",
        url="https://mcp.parallel.ai/mcp",
        tool="web_search",
        # No count or date fields: results are cut to the count, and dated
        # ones to the range, on arrival (compact_results).
        args=lambda q, n, r: {"objective": _objective(q, r), "search_queries": [q]},
        key_headers=lambda key: {"Authorization": f"Bearer {key}"},
    ),
    "tavily": SearchPreset(
        label="Tavily",
        url="https://mcp.tavily.com/mcp/",
        tool="tavily_search",
        args=lambda q, n, r: {
            "query": q,
            "max_results": n,
            **({"time_range": _RANGE[r]} if r in _RANGE else {}),
        },
        key_headers=lambda key: {"Authorization": f"Bearer {key}"},
        # Without it the server asks the client to sign in.
        keyless_headers={"X-Tavily-Access-Mode": "keyless"},
    ),
    "exa": SearchPreset(
        label="Exa",
        url="https://mcp.exa.ai/mcp",
        tool="web_search_exa",
        # No date field either: the range is asked for, and cut on arrival.
        args=lambda q, n, r: {
            "query": q,
            "objective": _objective(q, r),
            "numResults": n,
        },
        key_headers=lambda key: {"x-api-key": key},
    ),
}


def services(search: SearchConfig) -> list[str]:
    """The services a search asks, in order: all of them for ``auto``, the
    one picked, or none when search is off."""
    if search.provider == "auto":
        return list(AUTO_ORDER)
    return [search.provider] if search.provider in PRESETS else []


def server_config(search: SearchConfig, name: str) -> MCPServerConfig:
    """The MCP server of service ``name``, with its saved key if any."""
    preset = PRESETS[name]
    # Stripped here too: a hand-edited config.json skips the API's check.
    key = (search.keys.get(name) or "").strip()
    headers = preset.key_headers(key) if key else dict(preset.keyless_headers)
    # Through the system proxy unless set to direct: a search service is
    # always on the internet, where a proxy, if the machine has one, leads.
    return MCPServerConfig(
        name="search",
        url=preset.url,
        headers=headers,
        trust_env=search.trust_env is not False,
    )


def redact(text: str, search: SearchConfig) -> str:
    """``text`` with ``search``'s keys masked: a header httpx refuses comes
    back whole in its error, and errors reach the model and the page."""
    for key in search.keys.values():
        text = text.replace(key, "***")
    return text


def search_tool(search: SearchConfig) -> Tool | None:
    """The ``web_search`` tool over the configured service; None when off."""
    names = services(search)
    if not names:
        return None
    configs = {name: server_config(search, name) for name in names}
    # Only the search tool: the service's own fetch would rival web_fetch.
    servers = {
        name: replace(server_for(cfg), include_tools=[PRESETS[name].tool])
        for name, cfg in configs.items()
    }
    remembered: dict[str, float] = {}  # service -> until (monotonic)

    def order() -> list[str]:
        if len(names) == 1:
            # One service picked: a connection that fails to open is tried
            # once more — a hosted service drops the odd one.
            return names * 2
        now = _clock()
        first = [n for n in names if remembered.get(n, 0.0) > now]
        return first + [n for n in names if n not in first]

    async def ask(query: str, n: int, time_range: str | None) -> MCPToolResult:
        unreached: dict[str, str] = {}  # label -> why
        for name in order():
            preset, sent = PRESETS[name], False
            try:
                async with AsyncExitStack() as stack:
                    async with deadline(OPEN_TIMEOUT):
                        conn = await stack.enter_async_context(servers[name].session())
                    # Listed with only its search tool: none means it is gone.
                    if not conn.tools():
                        unreached[preset.label] = (
                            f"the service no longer offers {preset.tool}"
                        )
                        continue
                    sent = True
                    result = await conn.call(
                        preset.tool, preset.args(query, n, time_range)
                    )
            except Exception as exc:
                why = describe(exc, OPEN_TIMEOUT, configs[name])
                why = redact(why, search)
                # Never the search itself, here or elsewhere: a lost reply
                # may have run, and billed, it already.
                if sent:
                    raise ToolError(
                        f"{preset.label} search did not complete: {why}",
                        hint=_NETWORK_HINT,
                    ) from exc
                unreached[preset.label] = why
                continue
            if name == names[0]:
                remembered.clear()
            elif unreached:
                # Only when the ones before it were just tried: answering
                # from first place renews nothing, so they get tried again.
                remembered.clear()
                remembered[name] = _clock() + _REMEMBER
            return _refused(preset, result)
        if len(unreached) == 1:
            ((label, why),) = unreached.items()
            message = f"{label} search could not be reached: {why}"
        else:
            message = "No search service could be reached:\n" + "\n".join(
                f"- {label}: {why}" for label, why in unreached.items()
            )
        raise ToolError(message, hint=_NETWORK_HINT)

    # No tool-level retries (``ask`` has its own); the timeout bounds a server
    # that accepts the connection and never answers — with time for each
    # service after the first to open.
    @tool(
        name="web_search",
        retries=0,
        timeout=60 + OPEN_TIMEOUT * (len(order()) - 1),
        description=(
            "Search the web; returns the top results — title, url and an "
            "excerpt each.\n"
            f"{FENCE_NOTE}"
        ),
    )
    async def web_search(
        query: Annotated[str, "Search query."],
        max_results: Annotated[int, "Max results (1-20)."] = 5,
        time_range: Annotated[
            Literal["d", "w", "m", "y"] | None,
            "Optional recency filter: 'd'=past day, 'w'=week, 'm'=month, 'y'=year. "
            "Omit for no time limit; undated pages are kept.",
        ] = None,
    ) -> str:
        n = max(1, min(int(max_results), 20))
        since = (
            date.today() - timedelta(days=_RANGE_DAYS[time_range])
            if time_range in _RANGE_DAYS
            else None
        )
        result = await ask(query, n, time_range)
        return fence(
            compact_results(render_mcp_content(result.content), n, since=since)
        )

    return web_search


def _refused(preset: SearchPreset, result: MCPToolResult) -> MCPToolResult:
    """``result``, unless it is a refusal: then an error in the service's own
    words — a spent free allowance says what to do next — fenced like any
    answer of the service's."""
    text = render_mcp_content(result.content)
    refusal = text if result.is_error else _refusal(text)
    if refusal is None:
        return result
    raise ToolError(f"{preset.label} search failed:\n{fence(_clip(refusal, 1_000))}")


def compact_results(text: str, limit: int, *, since: date | None = None) -> str:
    """A search answer as numbered hits — title, url, excerpt — when it is
    JSON with a ``results`` list (Parallel, Tavily) or Exa's plain text;
    otherwise as it came. Either way within :data:`_ALL_CHARS`. A hit
    dated before ``since`` is left out. The bare url on its own line is what
    the chat turns into a link; ``[n]``, not ``n.``, so a hit is never
    mistaken for a list inside the excerpt before it."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    rows = data.get("results") if isinstance(data, dict) else _text_rows(text)
    if not isinstance(rows, list):
        return _clip(text, _ALL_CHARS)
    blocks: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or len(blocks) >= limit:
            continue
        if since is not None and (day := _date(row.get("publish_date"))):
            if day < since:
                continue
        title = str(row.get("title") or "").strip() or "(untitled)"
        excerpts = row.get("excerpts")
        body = (
            "\n".join(_FRONT_MATTER.sub("", str(e)) for e in excerpts)
            if isinstance(excerpts, list)
            else str(row.get("content") or row.get("snippet") or "")
        )
        block = f"[{len(blocks) + 1}] {title}"
        if url := str(row.get("url") or "").strip():
            block += f"\n{url}"
        if body := body.strip():
            block += f"\n{_clip(body, _HIT_CHARS)}"
        blocks.append(block)
    if not blocks:
        return "No results in that time range." if since and rows else "No results."
    return _clip("\n\n".join(blocks), _ALL_CHARS)


def _text_rows(text: str) -> list[dict[str, str]] | None:
    """Exa's answer — ``Title:``/``URL:``/``Published:`` lines, then the
    highlights — as rows; None when ``text`` is not in that shape."""
    heads = list(_TEXT_HIT.finditer(text))
    if not heads:
        return None
    ends = [head.start() for head in heads[1:]] + [len(text)]
    return [
        {
            "title": head[1],
            "url": head[2],
            "publish_date": head[3] or "",
            "content": text[head.end() : end].strip().removesuffix("---"),
        }
        for head, end in zip(heads, ends)
    ]


def _date(value: object) -> date | None:
    """The day an ISO date or datetime string names, or None (Exa's
    ``N/A`` included)."""
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _refusal(text: str) -> str | None:
    """The message of an answer that is a refusal, not results: Tavily's
    spent keyless allowance comes back as ``{"code": …, "message": …}``,
    flagged as no error."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and "results" not in data and "message" in data:
        return str(data["message"])
    return None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
