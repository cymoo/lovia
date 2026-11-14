"""The MCP servers of Settings → MCP, mounted on every agent whose switch is on.

Each server is opened the same way (docs/design/coding-features.md §6):

- its ``name`` prefixes its tools (``github__create_issue``);
- no reconnect and no retries — after a lost connection the server may
  already have done what was asked, and replaying the call inside one
  approved tool call could do it twice, past any approval;
- every tool asks first, with "always allow" for that tool in the chat —
  unless the workspace runs any shell command unasked ("full access"): a
  shell free to ``curl`` or ``gh`` reaches the same outside systems, so a
  prompt there would protect nothing (:func:`_permission`);
- one connection per run, closed when the run ends.

Unlike the core :class:`~lovia.plugins.mcp.MCP` plugin, a server that cannot
be reached within :data:`CONNECT_TIMEOUT` does not fail the run: the run goes
on without its tools, the chat says which server it was (an
``mcp_unavailable`` frame), the model is told, and Settings shows it as that
server's last connection. Servers open one after another — the known cost
of connecting per run.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from ..approvals import Verdict
from ..exceptions import UserError
from ..plugins.base import PluginInstance
from ..plugins.mcp import (
    MCPConnection,
    MCPServer,
    MCPServerStdio,
    MCPServerStreamableHTTP,
)
from ..run_context import RunContext
from ..tools import Tool
from .config.schema import MCPServerConfig

__all__ = ["CONNECT_TIMEOUT", "MCPBook", "MCPStatus", "SettingsMCP", "probe"]

log = logging.getLogger("lovia.web.mcp")

# How long one server may take to start and list its tools. Generous for an
# ``npx`` server's first download; a dead host fails long before it.
CONNECT_TIMEOUT = 30.0


@dataclass(frozen=True)
class MCPStatus:
    """How the last connection to a server went — from a run or a test."""

    ok: bool
    at: float
    tools: tuple[str, ...] = ()
    error: str | None = None


# What a run reports when it started without some servers: (name, why).
Unavailable = Callable[[RunContext[Any], list[tuple[str, str]]], None]


@dataclass
class MCPBook:
    """The last connection to each server, as Settings shows it — kept for
    the life of the process, not saved — and where a run reports servers it
    had to start without.

    A status counts for the server only as it was then: a run still on the
    agents built before an edit may report after it, and an edited server
    must not show how its old command fared. Switching it on or off is no
    such change."""

    unavailable: Unavailable | None = None
    _status: dict[str, tuple[MCPServerConfig, MCPStatus]] = field(default_factory=dict)

    def record(self, cfg: MCPServerConfig, status: MCPStatus) -> None:
        self._status[cfg.name] = (_as_connected(cfg), status)

    def get(self, cfg: MCPServerConfig) -> MCPStatus | None:
        seen = self._status.get(cfg.name)
        return seen[1] if seen is not None and seen[0] == _as_connected(cfg) else None


def _as_connected(cfg: MCPServerConfig) -> MCPServerConfig:
    return cfg if cfg.enabled else cfg.model_copy(update={"enabled": True})


def server_for(cfg: MCPServerConfig) -> MCPServer:
    """The core server for one entry, opened the way every one is."""
    if cfg.command is not None:
        return MCPServerStdio(
            name=cfg.name,
            command=cfg.command,
            args=list(cfg.args),
            env=dict(cfg.env) or None,
            auto_reconnect=False,
            retries=0,
            permission="ask",
        )
    assert cfg.url is not None  # one of the two, by validation
    return MCPServerStreamableHTTP(
        name=cfg.name,
        url=cfg.url,
        headers=dict(cfg.headers) or None,
        auto_reconnect=False,
        retries=0,
        permission="ask",
    )


@asynccontextmanager
async def _deadline(seconds: float) -> AsyncIterator[None]:
    """``asyncio.timeout`` where there is one. Never ``wait_for``, which runs
    the body in another task: the transport an open enters must be left by
    the task that entered it, or anyio refuses to close it."""
    if sys.version_info >= (3, 11):
        async with asyncio.timeout(seconds):
            yield
        return
    task = asyncio.current_task()
    assert task is not None
    fired = False

    def fire() -> None:
        nonlocal fired
        fired = True
        task.cancel()

    handle = asyncio.get_running_loop().call_later(seconds, fire)
    try:
        yield
    except asyncio.CancelledError:
        if fired:
            raise TimeoutError from None
        raise
    finally:
        handle.cancel()


async def connect(
    cfg: MCPServerConfig, timeout: float = CONNECT_TIMEOUT
) -> MCPConnection:
    """Open ``cfg`` and list its tools, within ``timeout`` seconds."""
    async with _deadline(timeout):
        return await server_for(cfg).open()


def describe(
    exc: BaseException,
    timeout: float = CONNECT_TIMEOUT,
    cfg: MCPServerConfig | None = None,
) -> str:
    """Why ``cfg`` could not be opened, in a line fit for a person."""
    # The SDK's transports fail inside task groups: the first real cause.
    while (inner := getattr(exc, "exceptions", None)) and isinstance(inner, tuple):
        exc = inner[0]
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return f"no answer within {timeout:g} s"
    if isinstance(exc, FileNotFoundError):
        if exc.filename:
            return f"command not found: {exc.filename}"
        # Unnamed (how the process is spawned decides): say what was
        # started, not which file was missing.
        if cfg is not None and cfg.command:
            return f"could not start {cfg.command}: {exc.strerror or exc}"
    if isinstance(exc, UserError):
        return str(exc)
    text = str(exc).strip()
    line = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return line if len(line) <= 300 else line[:299] + "…"


# What the strictest providers take as a tool name (OpenAI: ^[a-zA-Z0-9_-]{1,64}$).
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _usable(conn: MCPConnection) -> list[Tool]:
    """The connection's tools a model request can carry. One name over the
    limit (a long server name before a long tool name) would make the
    provider refuse every request of the run — such a tool is left out."""
    tools = []
    for t in conn.tools():
        if _TOOL_NAME.match(t.name):
            tools.append(t)
        else:
            log.warning("MCP tool %r left out: not a valid tool name", t.name)
    return tools


def _permission(name: str) -> Callable[[dict[str, Any], RunContext[Any]], Verdict]:
    """``name``'s verdict, judged per call against the run's workspace."""
    scope = (f"mcp:{name}",)

    def decide(_args: dict[str, Any], ctx: RunContext[Any]) -> Verdict:
        ws = ctx.workspace
        if ws is not None and ws.policy.shell_default == "allow":
            return Verdict("allow")
        return Verdict("ask", scopes=scope)

    return decide


def _raw_names(conn: MCPConnection, tools: Sequence[Tool]) -> tuple[str, ...]:
    prefix = f"{conn.prefix}__" if conn.prefix else ""
    return tuple(t.name.removeprefix(prefix) for t in tools)


async def probe(cfg: MCPServerConfig, timeout: float = CONNECT_TIMEOUT) -> MCPStatus:
    """Connect, list the tools, close: Settings' test button."""
    try:
        conn = await connect(cfg, timeout)
    except Exception as exc:  # noqa: BLE001 — every failure is the answer
        return MCPStatus(ok=False, at=time.time(), error=describe(exc, timeout, cfg))
    try:
        return MCPStatus(ok=True, at=time.time(), tools=_raw_names(conn, _usable(conn)))
    finally:
        await conn.close()


class SettingsMCP:
    """The plugin: every enabled server of Settings → MCP, for one agent."""

    name = "mcp"

    def __init__(
        self,
        servers: Sequence[MCPServerConfig],
        book: MCPBook,
        *,
        timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        self.servers = tuple(servers)
        self.book = book
        self.timeout = timeout

    async def setup(self, ctx: RunContext[Any]) -> PluginInstance:
        tools: list[Tool] = []
        opened: list[MCPConnection] = []
        failed: list[str] = []  # the servers' names

        async def aclose() -> None:
            for conn in reversed(opened):
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001 — best-effort teardown
                    log.debug(
                        "closing MCP server %s failed", conn.prefix, exc_info=True
                    )

        try:
            for cfg in self.servers:
                try:
                    conn = await connect(cfg, self.timeout)
                except Exception as exc:  # noqa: BLE001 — skipped, and said so
                    why = describe(exc, self.timeout, cfg)
                    log.warning("MCP server %s unavailable: %s", cfg.name, why)
                    failed.append(cfg.name)
                    self.book.record(
                        cfg, MCPStatus(ok=False, at=time.time(), error=why)
                    )
                    # Said now, not after the rest: one that hangs costs
                    # the whole timeout, and the chat should know meanwhile.
                    if self.book.unavailable is not None:
                        self.book.unavailable(ctx, [(cfg.name, why)])
                    continue
                opened.append(conn)
                usable = _usable(conn)
                tools.extend(replace(t, permission=_permission(t.name)) for t in usable)
                self.book.record(
                    cfg,
                    MCPStatus(ok=True, at=time.time(), tools=_raw_names(conn, usable)),
                )
        except BaseException:
            # Cancelled (a stop) while opening: what opened must not leak.
            await aclose()
            raise
        instructions = None
        if failed:
            # Names only: why it failed can be the server's own words, and
            # those don't belong in the system prompt.
            instructions = (
                f"These MCP servers could not be reached for this run, so their "
                f"tools are missing: {', '.join(failed)}. If the user asks for "
                "them, say the server did not connect."
            )
        return PluginInstance(tools=tools, instructions=instructions, aclose=aclose)
