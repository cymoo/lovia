"""Web search for lovia web: a hosted search MCP server, picked in Settings.

Each preset is one service's MCP endpoint, answering without an API key
within a free allowance (a key raises it). The ``web_search`` tool keeps one
shape — ``query``, ``max_results``, ``time_range`` — whichever service
answers, so the model, the prompt and the chat's view of the call never
change with the pick; each preset maps those onto its server's own tool.

A search connects when the model searches, not when a message starts: most
messages never search, and a connection costs about a second. Settings →
MCP can add any other search server; this one stays out of that list.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal

from ..exceptions import ToolError
from ..plugins.mcp import MCPToolResult, render_mcp_content
from ..tools import Tool, tool
from ..tools.fence import FENCE_NOTE, fence
from .config.schema import MCPServerConfig, SearchConfig
from .mcp import describe, server_for

# Per hit, and in all: hosted search answers run long (Parallel's excerpts
# reach 25k characters a call), and every one of them is the model's to read.
_HIT_CHARS = 1_500
_ALL_CHARS = 12_000
_TAVILY_RANGE = {"d": "day", "w": "week", "m": "month", "y": "year"}


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
        args=lambda q, n, r: {"objective": q, "search_queries": [q]},
        key_headers=lambda key: {"Authorization": f"Bearer {key}"},
    ),
    "tavily": SearchPreset(
        label="Tavily",
        url="https://mcp.tavily.com/mcp/",
        tool="tavily_search",
        args=lambda q, n, r: {
            "query": q,
            "max_results": n,
            **({"time_range": _TAVILY_RANGE[r]} if r in _TAVILY_RANGE else {}),
        },
        key_headers=lambda key: {"Authorization": f"Bearer {key}"},
        # Without it the server asks the client to sign in.
        keyless_headers={"X-Tavily-Access-Mode": "keyless"},
    ),
    "exa": SearchPreset(
        label="Exa",
        url="https://mcp.exa.ai/mcp",
        tool="web_search_exa",
        args=lambda q, n, r: {"query": q, "objective": q, "numResults": n},
        key_headers=lambda key: {"x-api-key": key},
    ),
}


def server_config(search: SearchConfig) -> MCPServerConfig | None:
    """The MCP server ``search`` picks, or None when search is off."""
    preset = PRESETS.get(search.provider)
    if preset is None:
        return None
    key = search.keys.get(search.provider)
    headers = preset.key_headers(key) if key else dict(preset.keyless_headers)
    return MCPServerConfig(
        name="search", url=preset.url, headers=headers, trust_env=search.trust_env
    )


def redact(text: str, search: SearchConfig) -> str:
    """``text`` with ``search``'s keys masked: a header httpx refuses comes
    back whole in its error, and errors reach the model and the page."""
    for key in search.keys.values():
        text = text.replace(key, "***")
    return text


def search_tool(search: SearchConfig) -> Tool | None:
    """The ``web_search`` tool over the configured service; None when off."""
    cfg = server_config(search)
    if cfg is None:
        return None
    preset = PRESETS[search.provider]
    # Only the search tool: the service's own fetch would rival web_fetch.
    server = replace(server_for(cfg), include_tools=[preset.tool])

    async def ask(args: dict[str, Any]) -> MCPToolResult:
        # A connection that fails to open is tried once more: a hosted
        # service drops the odd one, and a retry costs a second where a
        # failure costs the model a turn. Never the search itself — a lost
        # reply may have run, and billed, it already.
        for attempt in range(2):
            sent = False
            try:
                async with server.session() as conn:
                    sent = True
                    return await conn.call(preset.tool, args)
            except Exception:
                if sent or attempt:
                    raise
        raise AssertionError("unreachable")

    # No tool-level retries (``ask`` has its own); the timeout bounds a server
    # that accepts the connection and never answers.
    @tool(
        name="web_search",
        retries=0,
        timeout=60,
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
            "Omit for no time limit; not every search service applies it.",
        ] = None,
    ) -> str:
        n = max(1, min(int(max_results), 20))
        try:
            result = await ask(preset.args(query, n, time_range))
        except Exception as exc:  # noqa: BLE001 — the reason is the answer
            why = redact(describe(exc, cfg=cfg), search)
            raise ToolError(
                f"{preset.label} search could not be reached: {why}",
                hint="A network problem, not a bad query; Settings → Web search "
                "shows whether the service answers.",
            ) from exc
        text = render_mcp_content(result.content)
        # In its own words — a spent free allowance says what to do next —
        # fenced like any answer of the service's.
        refusal = text if result.is_error else _refusal(text)
        if refusal is not None:
            raise ToolError(
                f"{preset.label} search failed:\n{fence(_clip(refusal, 1_000))}"
            )
        return fence(compact_results(text, n))

    return web_search


def compact_results(text: str, limit: int) -> str:
    """A search answer as numbered hits — title, url, excerpt — when it is
    JSON with a ``results`` list (Parallel, Tavily); otherwise as it came.
    Either way within :data:`_ALL_CHARS`. The bare url on its own line is
    what the chat turns into a link."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return _clip(text, _ALL_CHARS)
    blocks: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or len(blocks) >= limit:
            continue
        title = str(row.get("title") or "").strip() or "(untitled)"
        excerpts = row.get("excerpts")
        body = (
            "\n".join(str(e) for e in excerpts)
            if isinstance(excerpts, list)
            else str(row.get("content") or row.get("snippet") or "")
        )
        block = f"{len(blocks) + 1}. {title}"
        if url := str(row.get("url") or "").strip():
            block += f"\n{url}"
        if body := body.strip():
            block += f"\n{_clip(body, _HIT_CHARS)}"
        blocks.append(block)
    return _clip("\n\n".join(blocks), _ALL_CHARS) if blocks else "No results."


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
