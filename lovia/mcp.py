"""MCP client integration.

Wraps the official `mcp` Python SDK so MCP tools appear as ordinary
:class:`Tool` instances on an :class:`Agent`. The dependency is optional: if
``mcp`` is not installed, importing this module is fine but constructing a
server raises a clear :class:`UserError`.

Supported transports:

* :class:`MCPServerStdio`: launch a subprocess and speak MCP over stdio.
* :class:`MCPServerStreamableHTTP`: connect to a streamable-HTTP MCP endpoint.

Each server, when connected, lists its tools and returns lovia :class:`Tool`
objects whose ``invoke`` callable proxies to the remote MCP server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .exceptions import UserError
from .tools import Tool


@dataclass
class MCPServer:
    """Base class for MCP servers. Use one of the concrete subclasses."""

    name: str | None = None
    # Filled in on connect; kept here so aclose can dispose them.
    _client: Any = field(default=None, repr=False, init=False)
    _exit_stack: Any = field(default=None, repr=False, init=False)

    async def connect(self) -> list[Tool]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def aclose(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._client = None


@dataclass
class MCPServerStdio(MCPServer):
    """Run a local MCP server as a subprocess and connect over stdio."""

    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None

    async def connect(self) -> list[Tool]:
        try:
            from contextlib import AsyncExitStack

            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - import guard
            raise UserError(
                "MCP support requires the optional 'mcp' package. Install with: pip install mcp"
            ) from exc

        params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception:
            await stack.aclose()
            raise
        self._exit_stack = stack
        self._client = session
        return await _list_remote_tools(session, prefix=self.name)


@dataclass
class MCPServerStreamableHTTP(MCPServer):
    """Connect to a remote MCP server over streamable HTTP."""

    url: str = ""
    headers: dict[str, str] | None = None

    async def connect(self) -> list[Tool]:
        try:
            from contextlib import AsyncExitStack

            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:  # pragma: no cover - import guard
            raise UserError(
                "MCP HTTP support requires the optional 'mcp' package. Install with: pip install mcp"
            ) from exc

        stack = AsyncExitStack()
        try:
            ctx = await stack.enter_async_context(streamablehttp_client(self.url, headers=self.headers))
            # streamablehttp_client yields (read, write, _get_session_id) in
            # recent versions; older versions yielded just (read, write).
            read, write = ctx[0], ctx[1]
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception:
            await stack.aclose()
            raise
        self._exit_stack = stack
        self._client = session
        return await _list_remote_tools(session, prefix=self.name)


async def _list_remote_tools(session: Any, *, prefix: str | None) -> list[Tool]:
    """Translate the MCP ``list_tools`` result into lovia :class:`Tool`s."""
    listing = await session.list_tools()
    tools: list[Tool] = []
    for entry in listing.tools:
        name = f"{prefix}__{entry.name}" if prefix else entry.name
        parameters = entry.inputSchema or {"type": "object", "properties": {}}

        async def invoke(args: dict[str, Any], ctx: Any, _name=entry.name) -> Any:
            result = await session.call_tool(_name, args)
            # MCP returns ``content`` as a list of typed blocks; flatten the
            # text bits into a single string for the model.
            parts: list[str] = []
            for block in getattr(result, "content", []) or []:
                if getattr(block, "type", None) == "text":
                    parts.append(block.text)
                else:
                    parts.append(json.dumps(getattr(block, "model_dump", lambda: str(block))()))
            return "\n".join(parts)

        tools.append(
            Tool(
                name=name,
                description=entry.description or "",
                parameters=parameters,
                invoke=invoke,
            )
        )
    return tools
