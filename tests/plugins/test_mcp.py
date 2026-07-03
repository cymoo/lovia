from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from lovia.exceptions import MCPError
from lovia.plugins.mcp import (
    MCP,
    MCPConnection,
    MCPServer,
    MCPToolResult,
    normalize_schema,
    render_mcp_content,
)
from lovia.run_context import RunContext
from lovia.tools import render_tool_result


# --------------------------------------------------------------------------- #
# Fakes (mcp-package-free: only attribute access is used by the implementation)
# --------------------------------------------------------------------------- #
def _tool(
    name: str, *, description: Any = "", input_schema: Any = None
) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description, inputSchema=input_schema)


def _page(tools: list[SimpleNamespace], next_cursor: str | None) -> SimpleNamespace:
    return SimpleNamespace(tools=tools, nextCursor=next_cursor)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _image(mime: str, data: str) -> SimpleNamespace:
    return SimpleNamespace(type="image", mimeType=mime, data=data)


def _embedded_text(text: str, uri: str = "file://x") -> SimpleNamespace:
    return SimpleNamespace(
        type="resource", resource=SimpleNamespace(text=text, uri=uri)
    )


def _result(
    content: list[SimpleNamespace], *, is_error: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(content=content, isError=is_error)


class FakeSession:
    def __init__(
        self,
        pages: list[SimpleNamespace] | None = None,
        *,
        results: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._pages = pages or [_page([], None)]
        self._results = results or {}
        self._error = error
        self.list_calls = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self, cursor: str | None = None) -> SimpleNamespace:
        self.list_calls += 1
        idx = 0 if cursor is None else int(cursor)
        return self._pages[idx]

    async def call_tool(self, name: str, args: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((name, args))
        if self._error is not None:
            raise self._error
        out = self._results.get(name, _result([_text(f"{name}-ok")]))
        return out(args) if callable(out) else out


def _ctx() -> RunContext[Any]:
    return RunContext(context=None, entries=[], agent=None)  # type: ignore[arg-type]


async def _loaded(session: FakeSession, **kwargs: Any) -> MCPConnection:
    conn = MCPConnection(transport=lambda: None, **kwargs)
    conn._session = session
    await conn._load_tools()
    return conn


# --------------------------------------------------------------------------- #
# Listing: pagination, naming, filtering, schema normalization
# --------------------------------------------------------------------------- #
async def test_pagination_and_prefix() -> None:
    session = FakeSession(
        pages=[
            _page([_tool("a"), _tool("b")], "1"),
            _page([_tool("c")], None),
        ]
    )
    conn = await _loaded(session, prefix="srv")
    assert [t.name for t in conn.tools()] == ["srv__a", "srv__b", "srv__c"]
    assert session.list_calls == 2


async def test_include_exclude_filter_on_raw_names() -> None:
    pages = [_page([_tool("a"), _tool("b"), _tool("c")], None)]
    inc = await _loaded(FakeSession(pages=pages), include_tools={"a", "c"})
    assert [t.name for t in inc.tools()] == ["a", "c"]

    exc = await _loaded(FakeSession(pages=pages), exclude_tools={"b"})
    assert [t.name for t in exc.tools()] == ["a", "c"]


async def test_schema_normalization() -> None:
    session = FakeSession(
        pages=[
            _page(
                [
                    _tool("x", input_schema=None),
                    _tool("y", input_schema={"type": "object"}),
                    _tool("z", input_schema={"properties": {"q": {"type": "string"}}}),
                ],
                None,
            )
        ]
    )
    conn = await _loaded(session)
    params = {t.name: t.parameters for t in conn.tools()}
    assert params["x"] == {"type": "object", "properties": {}}
    assert params["y"] == {"type": "object", "properties": {}}
    assert params["z"] == {"properties": {"q": {"type": "string"}}, "type": "object"}


def test_normalize_schema_unit() -> None:
    assert normalize_schema(None) == {"type": "object", "properties": {}}
    assert normalize_schema({}) == {"type": "object", "properties": {}}
    assert normalize_schema({"type": "object"}) == {"type": "object", "properties": {}}


# --------------------------------------------------------------------------- #
# Invocation + result rendering
# --------------------------------------------------------------------------- #
async def test_text_result_passthrough() -> None:
    session = FakeSession(
        pages=[_page([_tool("echo")], None)],
        results={"echo": lambda a: _result([_text("hello")])},
    )
    conn = await _loaded(session)
    tool = conn.tools()[0]
    ctx = _ctx()
    result = await tool.invoke({"x": 1}, ctx)
    assert isinstance(result, MCPToolResult) and result.is_error is False
    assert await render_tool_result(tool, result, ctx) == "hello"
    assert session.calls == [("echo", {"x": 1})]


async def test_iserror_marker() -> None:
    session = FakeSession(
        pages=[_page([_tool("boom")], None)],
        results={"boom": lambda a: _result([_text("nope")], is_error=True)},
    )
    conn = await _loaded(session)
    tool = conn.tools()[0]
    ctx = _ctx()
    result = await tool.invoke({}, ctx)
    assert result.is_error is True
    assert await render_tool_result(tool, result, ctx) == "[tool error] nope"


async def test_image_placeholder_never_base64() -> None:
    big = "A" * 10_000
    text = render_mcp_content([_image("image/png", big)])
    assert text.startswith("[image: image/png,")
    assert big not in text


async def test_embedded_text_resource_inlined() -> None:
    assert render_mcp_content([_embedded_text("doc body")]) == "doc body"


async def test_custom_result_renderer_receives_raw_result() -> None:
    def renderer(result: Any, ctx: RunContext[Any]) -> str:
        return f"custom:{len(result.content)}:{result.is_error}"

    session = FakeSession(
        pages=[_page([_tool("echo")], None)],
        results={"echo": lambda a: _result([_text("x"), _text("y")])},
    )
    conn = await _loaded(session, result_renderer=renderer)
    tool = conn.tools()[0]
    assert tool.result_renderer is renderer
    ctx = _ctx()
    result = await tool.invoke({}, ctx)
    assert await render_tool_result(tool, result, ctx) == "custom:2:False"


def test_mcp_tool_result_str_fallback() -> None:
    assert str(MCPToolResult(content=[_text("hi")])) == "hi"
    assert (
        str(MCPToolResult(content=[_text("bad")], is_error=True)) == "[tool error] bad"
    )


# --------------------------------------------------------------------------- #
# Reconnect + error handling
# --------------------------------------------------------------------------- #
async def test_reconnect_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    dead = FakeSession(error=ConnectionError("broken pipe"))
    healthy = FakeSession(results={"echo": lambda a: _result([_text("recovered")])})

    async def fake_open(self: MCPConnection) -> None:
        self._session = healthy

    monkeypatch.setattr(MCPConnection, "_open_session", fake_open)
    conn = MCPConnection(transport=lambda: None, auto_reconnect=True)
    conn._session = dead

    result = await conn._call("echo", {})
    assert str(result) == "recovered"
    assert healthy.calls == [("echo", {})]


async def test_no_reconnect_on_application_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opens = {"n": 0}

    async def fake_open(self: MCPConnection) -> None:
        opens["n"] += 1

    monkeypatch.setattr(MCPConnection, "_open_session", fake_open)
    conn = MCPConnection(transport=lambda: None, auto_reconnect=True)
    conn._session = FakeSession(error=ValueError("invalid params"))

    with pytest.raises(MCPError):
        await conn._call("x", {})
    assert opens["n"] == 0


async def test_no_reconnect_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    opens = {"n": 0}

    async def fake_open(self: MCPConnection) -> None:
        opens["n"] += 1

    monkeypatch.setattr(MCPConnection, "_open_session", fake_open)
    conn = MCPConnection(transport=lambda: None, auto_reconnect=False)
    conn._session = FakeSession(error=ConnectionError("dead"))

    with pytest.raises(MCPError):
        await conn._call("x", {})
    assert opens["n"] == 0


async def test_invoke_after_close_raises_mcp_error() -> None:
    conn = MCPConnection(transport=lambda: None)
    conn._session = FakeSession()
    await conn.close()
    assert conn._session is None
    with pytest.raises(MCPError):
        await conn._call("x", {})


async def test_refresh_tools_relists() -> None:
    session = FakeSession(pages=[_page([_tool("a")], None)])
    conn = await _loaded(session)
    assert [t.name for t in conn.tools()] == ["a"]
    conn.tools()  # cached read: no extra list call
    assert session.list_calls == 1
    session._pages = [_page([_tool("a"), _tool("b")], None)]
    refreshed = await conn.refresh_tools()
    assert [t.name for t in refreshed] == ["a", "b"]
    assert session.list_calls == 2


# --------------------------------------------------------------------------- #
# Lifecycle ownership (config vs persistent connection)
# --------------------------------------------------------------------------- #
class _FakeServer(MCPServer):
    def _make_transport(self):  # type: ignore[override]
        return lambda: None


async def test_config_open_is_owned_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_open(self: MCPConnection) -> None:
        self._session = FakeSession(pages=[_page([_tool("echo")], None)])

    monkeypatch.setattr(MCPConnection, "_open_session", fake_open)

    server = _FakeServer(name="srv")
    assert server.close_after_run is True
    conn = await server.open()
    assert conn.close_after_run is True
    assert [t.name for t in conn.tools()] == ["srv__echo"]
    # A live connection "opens" to itself (so it can be passed to MCP()).
    assert await conn.open() is conn
    await conn.close()


async def test_setup_failure_closes_owned_connections_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Server 1 (config → owned) opens fine, the persistent connection is
    # user-owned, server 3 fails: setup must close only the owned connection
    # before re-raising, so nothing leaks and the caller keeps theirs.
    async def fake_open(self: MCPConnection) -> None:
        self._session = FakeSession(pages=[_page([_tool("echo")], None)])

    monkeypatch.setattr(MCPConnection, "_open_session", fake_open)

    opened: list[MCPConnection] = []
    orig_open = _FakeServer.open

    async def tracking_open(self: _FakeServer) -> MCPConnection:
        conn = await orig_open(self)
        opened.append(conn)
        return conn

    monkeypatch.setattr(_FakeServer, "open", tracking_open)

    class _BoomServer(MCPServer):
        def _make_transport(self):  # type: ignore[override]
            return lambda: None

        async def open(self) -> MCPConnection:
            raise ConnectionError("boom")

    persistent = await _loaded(FakeSession(pages=[_page([_tool("keep")], None)]))
    plugin = MCP(_FakeServer(name="ok"), persistent, _BoomServer())

    with pytest.raises(ConnectionError, match="boom"):
        await plugin.setup()

    assert len(opened) == 1
    assert opened[0]._session is None  # owned connection closed, no leak
    assert persistent._session is not None  # user-owned connection untouched


async def test_persistent_session_not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_open(self: MCPConnection) -> None:
        self._session = FakeSession(pages=[_page([_tool("echo")], None)])

    monkeypatch.setattr(MCPConnection, "_open_session", fake_open)

    server = _FakeServer()
    async with server.session() as conn:
        assert conn.close_after_run is False
        assert conn._session is not None
        assert [t.name for t in conn.tools()] == ["echo"]
    assert conn._session is None  # closed on context exit
