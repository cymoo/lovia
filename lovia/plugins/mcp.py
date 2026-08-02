"""MCP client integration.

Wraps the official ``mcp`` Python SDK so MCP tools appear as ordinary
:class:`~lovia.tools.Tool` instances on an :class:`~lovia.Agent`. The dependency
is optional: importing this module is always fine, but constructing a transport
without ``mcp`` installed raises a clear :class:`~lovia.exceptions.UserError`.

Design mirrors :mod:`lovia.workspace`: the server object is **frozen config**, and
opening it yields a separate live :class:`MCPConnection`. This keeps per-run
usage concurrency-safe by construction (each run owns its own connection) while
still allowing an explicit, kept-alive connection across many runs.

Lifecycle::

    # Per-run (default): the runtime opens a fresh connection each run and
    # closes it afterwards. Just hand the server to the ``mcp`` plugin:
    agent = Agent(..., plugins=[MCP(MCPServerStdio(command="...", args=[...]))])

    # Persistent: open once, reuse across runs, close when done:
    server = MCPServerStdio(command="...", args=[...])
    async with server.session() as conn:
        agent = Agent(..., plugins=[MCP(conn)])
        await Runner.run(agent, "...")   # reuses the live connection
        await Runner.run(agent, "...")   # reused again

Supported transports:

* :class:`MCPServerStdio` — launch a subprocess and speak MCP over stdio.
* :class:`MCPServerStreamableHTTP` — connect to a streamable-HTTP MCP endpoint.

Caveats:

* Reconnect + retry is **at-least-once**: if the transport dies after the
  server executed the call but before the response arrived, the retry runs the
  side effect twice. Set ``auto_reconnect=False`` for non-idempotent tools.
* A persistent connection may be reused across *sequential* runs; sharing one
  across **concurrent** runs is unsupported — reconnection is not synchronized,
  so overlapping reconnects can leak a transport and fail the other run's
  in-flight calls.
* On Python < 3.12, combining a per-tool ``timeout`` with ``auto_reconnect``
  can leak the old transport on reconnect: ``asyncio.wait_for`` runs attempts
  in a separate task there, and anyio transports must be closed from the task
  that opened them. Python 3.12+ is unaffected.

Deliberate non-goals (keep the surface small): MCP prompts, resource browsing,
sampling, OAuth, heartbeats/subscriptions, and hosted MCP.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import (
    Annotated,
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Protocol,
    cast,
)

from pydantic import Field

from ..types import JsonObject
from ..exceptions import MCPError, ToolError, UserError
from ..run_context import RunContext
from ..tools import (
    ApprovalPredicate,
    Tool,
    ToolResultRenderer,
    render_tool_result,
    run_tool,
    tool,
    truncate_tool_output,
)
from .base import PluginInstance

logger = logging.getLogger(__name__)

_MCP_INSTALL_HINT = "Install the optional dependency with: pip install 'lovia[mcp]'"


# --------------------------------------------------------------------------- #
# Structured result + content rendering
# --------------------------------------------------------------------------- #
@dataclass
class MCPToolResult:
    """The structured value an MCP tool's ``invoke`` returns.

    ``content`` is the raw list of MCP content blocks (text/image/audio/
    embedded-resource). ``is_error`` mirrors the MCP ``isError`` flag.

    By default the MCP tool is given :func:`render_mcp_content` as its
    ``result_renderer``, which flattens this into a safe string. Pass a custom
    ``result_renderer`` on the server to receive this object untouched and
    decide exactly what the model sees.
    """

    content: list[Any] = field(default_factory=list)
    is_error: bool = False

    def __str__(self) -> str:
        return render_mcp_content(self.content, is_error=self.is_error)


def _approx_bytes(b64: str | None) -> int:
    """Approximate decoded byte length of a base64 string without decoding it."""
    if not b64:
        return 0
    n = len(b64)
    padding = b64.count("=", max(0, n - 2))
    return max(0, (n * 3) // 4 - padding)


def _human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num} B"


def _render_block(block: Any) -> str:
    """Render one MCP content block as a model-facing string.

    Text passes through; an embedded *text* resource is inlined; binary content
    (images, audio, blob resources) becomes a compact ``[kind: meta]``
    placeholder — never the raw base64, which would blow up the context.
    """
    btype = getattr(block, "type", None)
    if btype == "text":
        return getattr(block, "text", "") or ""
    if btype in ("image", "audio"):
        mime = getattr(block, "mimeType", None) or "application/octet-stream"
        size = _human_size(_approx_bytes(getattr(block, "data", None)))
        return f"[{btype}: {mime}, {size}]"
    if btype == "resource_link":
        uri = getattr(block, "uri", "") or ""
        return f"[resource link: {uri}]"
    if btype == "resource":
        resource = getattr(block, "resource", None)
        text = getattr(resource, "text", None)
        if text is not None:
            return str(text)
        uri = getattr(resource, "uri", "") or ""
        mime = getattr(resource, "mimeType", None) or "application/octet-stream"
        size = _human_size(_approx_bytes(getattr(resource, "blob", None)))
        return f"[resource: {uri}, {mime}, {size}]"
    # Unknown block type: serialise compactly, dropping bulky binary fields.
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        data = {k: v for k, v in dump().items() if k not in ("data", "blob")}
        return json.dumps(data, ensure_ascii=False)
    return str(block)


def render_mcp_content(content: list[Any], *, is_error: bool = False) -> str:
    """Flatten MCP content blocks into the string the model receives."""
    text = "\n".join(_render_block(b) for b in content)
    if is_error:
        return f"[tool error] {text}" if text else "[tool error]"
    return text


def _default_mcp_renderer(result: Any, ctx: RunContext[Any]) -> str:
    _ = ctx
    if isinstance(result, MCPToolResult):
        return render_mcp_content(result.content, is_error=result.is_error)
    return result if isinstance(result, str) else str(result)


# --------------------------------------------------------------------------- #
# Schema normalisation
# --------------------------------------------------------------------------- #
def normalize_schema(schema: object) -> JsonObject:
    """Coerce a (possibly loose) MCP input schema into a valid object schema.

    MCP servers emit ``None``, ``{}``, or ``{"type": "object"}`` without
    ``properties``. OpenAI-style function calling expects a well-formed object
    schema, so we guarantee one without otherwise touching the semantics.
    """
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    out = cast(JsonObject, dict(schema))
    if "type" not in out:
        out["type"] = "object"
    if out.get("type") == "object" and not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    return out


# --------------------------------------------------------------------------- #
# Connection error classification (for auto-reconnect)
# --------------------------------------------------------------------------- #
def _is_connection_error(exc: BaseException) -> bool:
    """True only for genuine transport/connection failures.

    Deliberately excludes cancellation, timeouts, and protocol/application
    errors (bad params, unknown tool) — reconnecting on those would mask real
    bugs and risk duplicate side effects.
    """
    if isinstance(exc, (asyncio.CancelledError, asyncio.TimeoutError, TimeoutError)):
        return False
    conn_types: tuple[type[BaseException], ...] = (ConnectionError, BrokenPipeError)
    try:  # anyio ships with mcp; its stream errors signal a dead transport.
        import anyio

        conn_types = conn_types + (
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
            anyio.EndOfStream,
        )
    except Exception:  # noqa: BLE001 - anyio absent → fall back to stdlib types
        pass
    return isinstance(exc, conn_types)


# --------------------------------------------------------------------------- #
# Live connection
# --------------------------------------------------------------------------- #
@dataclass
class MCPConnection:
    """A live MCP session plus the lovia tools bound to it.

    Created by :meth:`MCPServer.open` / :meth:`MCPServer.session`; not usually
    constructed directly. Implements the same minimal surface the runtime needs
    from a server (``close_after_run`` + :meth:`open`), so a persistent connection
    can be passed directly to :class:`MCP`.
    """

    transport: Callable[[], Any]
    prefix: str | None = None
    include_tools: set[str] | None = None
    exclude_tools: set[str] | None = None
    needs_approval: bool | ApprovalPredicate = False
    retries: int | None = None
    timeout: float | None = None
    max_output_chars: int | None = None
    result_renderer: ToolResultRenderer | None = None
    auto_reconnect: bool = True
    close_after_run: bool = False
    defer: bool = False
    _session: Any = field(default=None, repr=False)
    _exit_stack: Any = field(default=None, repr=False)
    _tools: list[Tool] | None = field(default=None, repr=False)

    # -- MCPServerLike adapter: a live connection "opens" to itself ---------- #
    async def open(self) -> "MCPConnection":
        return self

    async def __aenter__(self) -> "MCPConnection":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # -- tools --------------------------------------------------------------- #
    def tools(self) -> list[Tool]:
        if self._tools is None:
            raise MCPError(
                "MCP connection has no tools loaded.",
                hint="Open the connection before requesting its tools.",
            )
        return list(self._tools)

    async def refresh_tools(self) -> list[Tool]:
        """Re-list the server's tools and rebuild the cached lovia tools."""
        await self._load_tools(force=True)
        return list(self._tools or [])

    # -- lifecycle ----------------------------------------------------------- #
    async def close(self) -> None:
        stack = self._exit_stack
        self._exit_stack = None
        self._session = None
        if stack is not None:
            await stack.aclose()

    async def _open_session(self) -> None:
        try:
            from mcp import ClientSession
        except ImportError as exc:  # pragma: no cover - import guard
            raise UserError(
                "MCP support requires the optional 'mcp' package.",
                hint=_MCP_INSTALL_HINT,
            ) from exc
        stack = AsyncExitStack()
        try:
            transport = await stack.enter_async_context(self.transport())
            read, write = transport[0], transport[1]
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._exit_stack = stack
        self._session = session

    async def _reconnect(self) -> None:
        # Must run in the task that opened the session: anyio transports bind
        # their cancel scopes to the entering task (see module Caveats for the
        # Python < 3.12 wait_for interaction).
        old = self._exit_stack
        self._session = None
        self._exit_stack = None
        if old is not None:
            try:
                await old.aclose()
            except Exception:  # noqa: BLE001 - the old transport is already dead
                pass
        await self._open_session()

    def _require_session(self) -> Any:
        if self._session is None:
            raise MCPError(
                "MCP connection is closed.",
                hint="Open it per run, or keep it alive with 'async with server.session()'.",
            )
        return self._session

    # -- listing ------------------------------------------------------------- #
    async def _list_all_tools(self) -> list[Any]:
        session = self._require_session()
        out: list[Any] = []
        cursor: str | None = None
        while True:
            result = await session.list_tools(cursor=cursor)
            out.extend(result.tools)
            cursor = getattr(result, "nextCursor", None)
            if not cursor:
                break
        return out

    def _keep(self, name: str) -> bool:
        if self.include_tools is not None and name not in self.include_tools:
            return False
        if self.exclude_tools is not None and name in self.exclude_tools:
            return False
        return True

    async def _load_tools(self, *, force: bool = False) -> None:
        if self._tools is not None and not force:
            return
        renderer = self.result_renderer or _default_mcp_renderer
        tools: list[Tool] = []
        for entry in await self._list_all_tools():
            raw_name = entry.name
            if not self._keep(raw_name):
                continue
            display = f"{self.prefix}__{raw_name}" if self.prefix else raw_name
            tools.append(
                Tool(
                    name=display,
                    description=getattr(entry, "description", None) or "",
                    parameters=normalize_schema(getattr(entry, "inputSchema", None)),
                    invoke=self._make_invoke(raw_name),
                    needs_approval=self.needs_approval,
                    retries=self.retries,
                    timeout=self.timeout,
                    max_output_chars=self.max_output_chars,
                    result_renderer=renderer,
                )
            )
        self._tools = tools

    # -- invocation ---------------------------------------------------------- #
    def _make_invoke(
        self, tool_name: str
    ) -> Callable[[dict[str, Any], RunContext[Any]], Any]:
        async def invoke(args: dict[str, Any], ctx: RunContext[Any]) -> MCPToolResult:
            _ = ctx
            return await self._call(tool_name, args)

        return invoke

    async def _invoke_once(self, tool_name: str, args: dict[str, Any]) -> MCPToolResult:
        session = self._require_session()
        result = await session.call_tool(tool_name, args)
        content = list(getattr(result, "content", None) or [])
        is_error = bool(getattr(result, "isError", False))
        return MCPToolResult(content=content, is_error=is_error)

    async def _call(self, tool_name: str, args: dict[str, Any]) -> MCPToolResult:
        try:
            return await self._invoke_once(tool_name, args)
        except (MCPError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - normalised into MCPError below
            if self.auto_reconnect and _is_connection_error(exc):
                try:
                    await self._reconnect()
                except asyncio.CancelledError:
                    raise
                except Exception as rexc:  # noqa: BLE001 - normalised below
                    raise MCPError(
                        f"MCP tool {tool_name!r} failed: {exc}; "
                        f"reconnect also failed: {rexc}",
                        hint="The MCP server connection could not be recovered.",
                        tool_name=tool_name,
                    ) from rexc
                try:
                    return await self._invoke_once(tool_name, args)
                except asyncio.CancelledError:
                    raise
                except Exception as exc2:  # noqa: BLE001 - normalised below
                    raise MCPError(
                        f"MCP tool {tool_name!r} failed after reconnect: {exc2}",
                        hint="The MCP server connection could not be recovered.",
                        tool_name=tool_name,
                    ) from exc2
            raise MCPError(
                f"MCP tool {tool_name!r} failed: {exc}",
                hint="Check that the MCP server is running and reachable.",
                tool_name=tool_name,
            ) from exc


# --------------------------------------------------------------------------- #
# Server config (frozen, factory)
# --------------------------------------------------------------------------- #
class MCPServerLike(Protocol):
    """What the :class:`MCP` plugin needs from each server entry.

    Satisfied by both :class:`MCPServer` config (``close_after_run=True``) and a
    live :class:`MCPConnection` (``close_after_run=False``).
    """

    # Read-only so frozen-dataclass configs (e.g. ``MCPServer``) satisfy the
    # protocol. A plain ``close_after_run: bool`` would demand a *settable*
    # attribute, which a ``@dataclass(frozen=True)`` field is not.
    @property
    def close_after_run(self) -> bool: ...

    # Whether this server's tools are deferred (searchable via
    # ``search_mcp_tools`` instead of listed on the agent). Same read-only
    # rationale as ``close_after_run``.
    @property
    def defer(self) -> bool: ...

    async def open(self) -> MCPConnection: ...


@dataclass(frozen=True, kw_only=True)
class MCPServer:
    """Base config for an MCP server. Use a concrete transport subclass.

    Immutable configuration only — opening it yields a separate
    :class:`MCPConnection` that owns the live session. Keyword-only on
    purpose: the first positional slot would otherwise be ``name``, so
    ``MCPServerStdio("npx")`` would silently configure a prefix instead of
    a command.
    """

    name: str | None = None
    include_tools: list[str] | None = None
    exclude_tools: list[str] | None = None
    needs_approval: bool | ApprovalPredicate = False
    retries: int | None = None
    timeout: float | None = None
    # Cap (in chars) on each tool's rendered output — MCP servers are the
    # likeliest source of huge text payloads (inlined embedded resources).
    # ``None`` defers to the agent's ``max_tool_output_chars``.
    max_output_chars: int | None = None
    result_renderer: ToolResultRenderer | None = None
    auto_reconnect: bool = True
    close_after_run: bool = True
    # Withhold this server's tools from the agent's tool list: the model
    # discovers them with ``search_mcp_tools`` and invokes them with
    # ``call_mcp_tool``. Only the tool *names* reach the system prompt, so a
    # large server stops crowding the context with unused schemas.
    defer: bool = False

    def _make_transport(self) -> Callable[[], Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def open(self) -> MCPConnection:
        """Open a fresh connection owned by the caller (the runtime, per run)."""
        return await self._open_connection(close_after_run=self.close_after_run)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[MCPConnection]:
        """Open a persistent connection for reuse across multiple runs."""
        conn = await self._open_connection(close_after_run=False)
        try:
            yield conn
        finally:
            await conn.close()

    async def _open_connection(self, *, close_after_run: bool) -> MCPConnection:
        conn = MCPConnection(
            transport=self._make_transport(),
            prefix=self.name,
            include_tools=set(self.include_tools) if self.include_tools else None,
            exclude_tools=set(self.exclude_tools) if self.exclude_tools else None,
            needs_approval=self.needs_approval,
            retries=self.retries,
            timeout=self.timeout,
            max_output_chars=self.max_output_chars,
            result_renderer=self.result_renderer,
            auto_reconnect=self.auto_reconnect,
            close_after_run=close_after_run,
            defer=self.defer,
        )
        try:
            await conn._open_session()
            await conn._load_tools()
        except BaseException:
            await conn.close()
            raise
        return conn


@dataclass(frozen=True, kw_only=True)
class MCPServerStdio(MCPServer):
    """Run a local MCP server as a subprocess and connect over stdio."""

    command: str
    args: list[str] | None = None
    env: dict[str, str] | None = None

    def _make_transport(self) -> Callable[[], Any]:
        try:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - import guard
            raise UserError(
                "MCP support requires the optional 'mcp' package.",
                hint=_MCP_INSTALL_HINT,
            ) from exc
        params = StdioServerParameters(
            command=self.command, args=list(self.args or []), env=self.env
        )
        return lambda: stdio_client(params)


@dataclass(frozen=True, kw_only=True)
class MCPServerStreamableHTTP(MCPServer):
    """Connect to a remote MCP server over streamable HTTP."""

    url: str
    headers: dict[str, str] | None = None

    def _make_transport(self) -> Callable[[], Any]:
        try:
            from importlib import import_module

            module = import_module("mcp.client.streamable_http")
        except ImportError as exc:  # pragma: no cover - import guard
            raise UserError(
                "MCP HTTP support requires the optional 'mcp' package.",
                hint=_MCP_INSTALL_HINT,
            ) from exc
        # The factory was renamed across mcp releases; accept either spelling.
        client = getattr(module, "streamable_http_client", None) or getattr(
            module, "streamablehttp_client", None
        )
        if client is None:  # pragma: no cover - very old/new mcp
            raise UserError(
                "Installed 'mcp' has no streamable-HTTP client.",
                hint="Upgrade with: pip install -U mcp",
            )
        url, headers = self.url, self.headers
        return lambda: client(url, headers=headers)


# --------------------------------------------------------------------------- #
# Deferred tools: keyword search + generic invoker
# --------------------------------------------------------------------------- #
def _deferred_instructions(by_server: list[tuple[str | None, list[str]]]) -> str:
    """System-prompt fragment: name every deferred tool, and nothing more.

    Names alone are enough for the model to know a capability exists (and to
    search for the exact one); the costly part — descriptions and parameter
    schemas — stays behind ``search_mcp_tools``.
    """
    lines = [
        "## Deferred MCP tools",
        "The MCP tools listed below exist but are not in your tool list. To "
        "use one: look it up with search_mcp_tools (keywords or its exact "
        "name) to get its description and parameters schema, then invoke it "
        "with call_mcp_tool. Search before concluding a capability is "
        "missing.",
    ]
    for prefix, names in by_server:
        label = prefix or "unnamed server"
        lines.append(f"- {label} ({len(names)} tools): {', '.join(names)}")
    return "\n".join(lines)


def _make_search_tool(catalog: dict[str, Tool]) -> Tool:
    @tool(
        name="search_mcp_tools",
        description=(
            "Find deferred MCP tools and return their full definitions "
            "(name, description, parameters JSON schema).\n"
            "- Matches keywords against tool names and descriptions; an "
            "exact tool name ranks first.\n"
            "- Always fetch a tool's definition here before invoking it "
            "with call_mcp_tool, so the arguments match its schema."
        ),
    )
    async def search_mcp_tools(
        query: Annotated[
            str,
            Field(
                min_length=1,
                description="Keywords (or an exact tool name) to search for.",
            ),
        ],
        max_results: Annotated[
            int,
            Field(default=5, ge=1, le=20, description="Maximum matches returned."),
        ] = 5,
    ) -> str:
        tokens = query.lower().split()
        scored: list[tuple[int, str, Tool]] = []
        for name, target in catalog.items():
            name_l, desc_l = name.lower(), (target.description or "").lower()
            score = 0
            for tok in tokens:
                if tok == name_l:
                    score += 3
                elif tok in name_l:
                    score += 2
                if tok in desc_l:
                    score += 1
            if score:
                scored.append((-score, name, target))
        scored.sort(key=lambda item: (item[0], item[1]))
        if not scored:
            return (
                f"No deferred MCP tool matched {query!r} ({len(catalog)} "
                "available — the full name list is in your instructions; "
                "try other keywords)."
            )
        shown = scored[:max_results]
        text = json.dumps(
            [
                {
                    "name": name,
                    "description": target.description,
                    "parameters": target.parameters,
                }
                for _, name, target in shown
            ],
            ensure_ascii=False,
        )
        if len(scored) > len(shown):
            text += (
                f"\n({len(scored) - len(shown)} more matches not shown; "
                "refine the query or raise max_results.)"
            )
        return text

    return search_mcp_tools


def _make_call_tool(catalog: dict[str, Tool]) -> Tool:
    def _needs(args: dict[str, Any], ctx: RunContext[Any]) -> bool:
        target = catalog.get(str(args.get("tool", "")))
        if target is None:
            return False  # the call fails with unknown-tool before running
        return target.requires_approval(dict(args.get("arguments") or {}), ctx)

    @tool(
        name="call_mcp_tool",
        description=(
            "Invoke a deferred MCP tool by its exact name.\n"
            "- arguments must match the parameters schema that "
            "search_mcp_tools returned for the tool.\n"
            "- Only deferred MCP tools are reachable here; regular tools "
            "are called directly."
        ),
        needs_approval=_needs,
    )
    async def call_mcp_tool(
        ctx: RunContext[Any],
        tool: Annotated[str, "Exact tool name, as returned by search_mcp_tools."],
        arguments: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description="Arguments matching the tool's parameters schema.",
            ),
        ] = None,
    ) -> str:
        target = catalog.get(tool)
        if target is None:
            near = difflib.get_close_matches(tool, list(catalog), n=3, cutoff=0.4)
            raise ToolError(
                f"unknown deferred MCP tool: {tool!r}",
                hint=(
                    f"Did you mean: {', '.join(near)}?"
                    if near
                    else "Use search_mcp_tools to discover tools."
                ),
                tool_name="call_mcp_tool",
            )
        # Full delegation: run_tool honors the underlying tool's retries and
        # timeout; rendering + truncation below honor its renderer and output
        # cap, so a deferred tool behaves exactly like its exposed self.
        # (Approval is the runner's job and is delegated via ``_needs``.)
        raw = await run_tool(target, dict(arguments or {}), ctx)
        rendered = await render_tool_result(target, raw, ctx)
        if target.max_output_chars is not None:
            rendered = truncate_tool_output(rendered, target.max_output_chars)
        return rendered

    return call_mcp_tool


# --------------------------------------------------------------------------- #
# Plugin factory
# --------------------------------------------------------------------------- #
class MCP:
    """Mount one or more MCP servers' tools on an agent, as a plugin.

    Each ``server`` is opened once per run; a config :class:`MCPServer` is closed
    when the run ends, while a live :class:`MCPConnection` (from
    ``async with server.session()``) is left open for its owner. Disambiguate
    overlapping tool names with ``MCPServer.name`` (which prefixes ``name__tool``).

    Example::

        from lovia.plugins.mcp import MCP, MCPServerStdio

        agent = Agent(
            ...,
            plugins=[MCP(MCPServerStdio(command="uvx", args=["mcp-server-fetch"]))],
        )

    A server with ``defer=True`` contributes **no** tools directly: the agent
    instead gets ``search_mcp_tools`` + ``call_mcp_tool`` (one pair, shared by
    all deferred servers of this plugin) and a system-prompt line naming the
    deferred tools. Use it for large servers whose schemas would crowd the
    context; deferred and regular servers mix freely::

        plugins=[MCP(
            MCPServerStdio(command="...", name="github", defer=True),  # 60 tools
            MCPServerStdio(command="..."),                             # 3 tools
        )]
    """

    name: str

    def __init__(self, *servers: MCPServerLike, name: str = "mcp") -> None:
        self.servers = tuple(servers)
        self.name = name

    async def setup(self) -> PluginInstance:
        exposed: list[Tool] = []
        deferred: dict[str, Tool] = {}
        deferred_by_server: list[tuple[str | None, list[str]]] = []
        closers: list[Callable[[], Awaitable[None]]] = []

        async def aclose() -> None:
            for close in reversed(closers):
                try:
                    await close()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    logger.debug("mcp.close failed during teardown", exc_info=True)

        try:
            for server in self.servers:
                conn = await server.open()
                if server.close_after_run:
                    closers.append(conn.close)
                if server.defer:
                    names: list[str] = []
                    for t in conn.tools():
                        if t.name in deferred:
                            raise UserError(
                                f"deferred MCP tool name clash: {t.name!r} is "
                                "provided by two servers.",
                                hint=(
                                    "Set MCPServer(name=...) to prefix one "
                                    "server's tools."
                                ),
                            )
                        deferred[t.name] = t
                        names.append(t.name)
                    deferred_by_server.append((conn.prefix, names))
                else:
                    exposed.extend(conn.tools())
            # The catalog and the tool list form one namespace in the model's
            # eyes — a name on both sides would make call_mcp_tool and the
            # direct call subtly diverge, so refuse it outright.
            overlap = sorted(deferred.keys() & {t.name for t in exposed})
            if overlap:
                raise UserError(
                    f"deferred MCP tool name clash with exposed tools: "
                    f"{', '.join(overlap)}.",
                    hint="Set MCPServer(name=...) to prefix one server's tools.",
                )
        except BaseException:
            # A later server failed to open (or validation failed): the runner
            # never receives the instance, so close the connections opened so
            # far here — otherwise their transports (stdio subprocesses) would
            # leak.
            await aclose()
            raise
        instructions: str | None = None
        if deferred:
            exposed.append(_make_search_tool(deferred))
            exposed.append(_make_call_tool(deferred))
            instructions = _deferred_instructions(deferred_by_server)
        return PluginInstance(tools=exposed, instructions=instructions, aclose=aclose)


__all__ = [
    "MCP",
    "MCPConnection",
    "MCPError",
    "MCPServer",
    "MCPServerLike",
    "MCPServerStdio",
    "MCPServerStreamableHTTP",
    "MCPToolResult",
    "normalize_schema",
    "render_mcp_content",
]
