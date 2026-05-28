"""attach_sandbox integration."""

from __future__ import annotations

from pathlib import Path

from lovia import Agent
from lovia.sandbox import (
    LocalSandbox,
    LocalSandboxProvider,
    attach_sandbox,
    sandbox_tools,
)


def _base_agent() -> Agent:
    return Agent(name="coder", instructions="be brief", model="openai:gpt-4o-mini")


def test_attach_returns_clone(tmp_path: Path) -> None:
    base = _base_agent()
    p = LocalSandboxProvider(root_base=tmp_path)
    new = attach_sandbox(base, p)
    assert new is not base
    assert new.name == base.name
    assert len(new.tools) >= 6


def test_attach_preserves_existing_tools(tmp_path: Path) -> None:
    from lovia.tools import tool

    @tool
    def hello() -> str:
        """Say hi."""
        return "hi"

    base = Agent(
        name="coder", instructions="x", model="openai:gpt-4o-mini", tools=[hello]
    )
    p = LocalSandboxProvider(root_base=tmp_path)
    new = attach_sandbox(base, p)
    names = {t.name for t in new.tools}
    assert "hello" in names
    assert "run" in names


def test_attach_with_bare_sandbox(tmp_path: Path) -> None:
    base = _base_agent()
    sb = LocalSandbox(root=tmp_path)
    new = attach_sandbox(base, sb)
    assert {"read_file", "write_file", "run"}.issubset({t.name for t in new.tools})


def test_attach_audit_default_present(tmp_path: Path) -> None:
    base = _base_agent()
    p = LocalSandboxProvider(root_base=tmp_path)
    new = attach_sandbox(base, p)
    run_tool = next(t for t in new.tools if t.name == "run")
    assert len(run_tool.policies) >= 1


def test_attach_audit_disabled(tmp_path: Path) -> None:
    base = _base_agent()
    p = LocalSandboxProvider(root_base=tmp_path)
    new = attach_sandbox(base, p, audit=None)
    run_tool = next(t for t in new.tools if t.name == "run")
    assert len(run_tool.policies) == 0


async def test_attach_tools_resolve_via_provider(tmp_path: Path) -> None:
    """End-to-end: invoking a sandbox tool acquires the session sandbox lazily."""
    from types import SimpleNamespace

    from tests.sandbox.conftest import make_ctx

    base = _base_agent()
    p = LocalSandboxProvider(root_base=tmp_path)
    new = attach_sandbox(base, p, audit=None)
    write = next(t for t in new.tools if t.name == "write_file")
    ctx = make_ctx("s-lazy")
    ctx.agent = SimpleNamespace(name="coder")  # type: ignore[assignment]
    # Provider untouched before first tool call.
    assert await p.get("s-lazy") is None
    await write.invoke({"path": "f.txt", "content": "ok"}, ctx)
    sb = await p.get("s-lazy")
    assert sb is not None
    assert (await sb.read("f.txt")) == b"ok"
    await p.shutdown()


def test_attach_matches_sandbox_tools(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    base = _base_agent()
    new = attach_sandbox(base, p)
    direct = {t.name for t in sandbox_tools(p)}
    attached = {t.name for t in new.tools}
    assert direct.issubset(attached)
