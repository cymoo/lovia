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

Deliberate non-goals (keep the surface small): MCP prompts, resource browsing,
sampling, OAuth, heartbeats/subscriptions, and hosted MCP.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Protocol,
    cast,
)

from ..types import JsonObject
from ..exceptions import MCPError, UserError
from ..run_context import RunContext
from ..tools import ApprovalPredicate, Tool, ToolResultRenderer
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
    """

    name: str

    def __init__(self, *servers: MCPServerLike, name: str = "mcp") -> None:
        self.servers = tuple(servers)
        self.name = name

    async def setup(self) -> PluginInstance:
        tools: list[Tool] = []
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
                tools.extend(conn.tools())
        except BaseException:
            # A later server failed to open: the runner never receives the
            # instance, so close the connections opened so far here — otherwise
            # their transports (stdio subprocesses) would leak.
            await aclose()
            raise
        return PluginInstance(tools=tools, aclose=aclose)


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
