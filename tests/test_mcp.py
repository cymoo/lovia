from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from lovia.mcp import _list_remote_tools
from lovia.run_context import RunContext


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="first",
                    description="First tool",
                    inputSchema={"type": "object", "properties": {}},
                ),
                SimpleNamespace(
                    name="second",
                    description="Second tool",
                    inputSchema=None,
                ),
            ]
        )

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=f"{name}:{args['x']}")]
        )


async def test_mcp_tools_capture_names_and_apply_policies() -> None:
    session = _FakeSession()
    tools = await _list_remote_tools(
        session,
        prefix="srv",
        needs_approval=True,
        retries=3,
        timeout=5,
    )

    assert [t.name for t in tools] == ["srv__first", "srv__second"]
    assert tools[0].needs_approval is True
    assert tools[0].retries == 3
    assert tools[0].timeout == 5

    ctx = RunContext(context=None, messages=[], agent=None)  # type: ignore[arg-type]
    first = await tools[0].invoke({"x": "a"}, ctx)
    second = await tools[1].invoke({"x": "b"}, ctx)

    assert first == "first:a"
    assert second == "second:b"
    assert session.calls == [("first", {"x": "a"}), ("second", {"x": "b"})]
