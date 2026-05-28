"""sandbox_tools: tool factory + apply_patch."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


from lovia.sandbox import (
    ExecLimits,
    LocalSandbox,
    LocalSandboxProvider,
    sandbox_tools,
)
from lovia.sandbox.tools import _apply_unified_diff

from .conftest import make_ctx


def _agent_ctx(session_id: str = "s1"):
    ctx = make_ctx(session_id)
    ctx.agent = SimpleNamespace(name="test")  # type: ignore[assignment]
    return ctx


async def test_sandbox_tools_default_set(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    tools = sandbox_tools(sb)
    names = {t.name for t in tools}
    assert names == {
        "read_file",
        "write_file",
        "list_dir",
        "glob_paths",
        "apply_patch",
        "run",
    }


async def test_sandbox_tools_include_exclude(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    tools = sandbox_tools(sb, include=["read_file", "write_file"])
    assert {t.name for t in tools} == {"read_file", "write_file"}
    tools = sandbox_tools(sb, exclude=["run"])
    names = {t.name for t in tools}
    assert "run" not in names
    assert "read_file" in names


async def test_read_write_tools(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    tools = {t.name: t for t in sandbox_tools(sb)}
    ctx = _agent_ctx()
    out = await tools["write_file"].invoke({"path": "a.txt", "content": "hi"}, ctx)
    assert "2 bytes" in out
    text = await tools["read_file"].invoke({"path": "a.txt"}, ctx)
    assert text == "hi"


async def test_list_dir_tool(seeded_root: Path) -> None:
    sb = LocalSandbox(root=seeded_root)
    tools = {t.name: t for t in sandbox_tools(sb)}
    entries = await tools["list_dir"].invoke({"path": "."}, _agent_ctx())
    names = {e["name"] for e in entries}
    assert names == {"a.txt", "sub"}


async def test_glob_tool(seeded_root: Path) -> None:
    sb = LocalSandbox(root=seeded_root)
    tools = {t.name: t for t in sandbox_tools(sb)}
    matches = await tools["glob_paths"].invoke({"pattern": "**/*.py"}, _agent_ctx())
    assert matches == ["sub/b.py"]


async def test_run_tool(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    tools = {t.name: t for t in sandbox_tools(sb, audit=None)}
    result = await tools["run"].invoke({"cmd": "echo hi"}, _agent_ctx())
    assert result["exit_code"] == 0
    assert "hi" in result["stdout"]


async def test_run_tool_respects_exec_limits(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    tools = {
        t.name: t
        for t in sandbox_tools(sb, audit=None, exec_limits=ExecLimits(timeout=0.3))
    }
    result = await tools["run"].invoke({"cmd": "sleep 2"}, _agent_ctx())
    assert result["timed_out"] is True


async def test_provider_lazy_resolution(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    tools = {t.name: t for t in sandbox_tools(p)}
    ctx = _agent_ctx("session-abc")
    # Provider hasn't been touched yet
    assert await p.get("session-abc") is None
    await tools["write_file"].invoke({"path": "x", "content": "1"}, ctx)
    sb = await p.get("session-abc")
    assert sb is not None
    assert (await sb.read("x")) == b"1"
    await p.shutdown()


async def test_provider_uses_default_session_id(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    tools = {t.name: t for t in sandbox_tools(p)}
    ctx = _agent_ctx(session_id=None)
    await tools["write_file"].invoke({"path": "x", "content": "1"}, ctx)
    sb = await p.get("default")
    assert sb is not None
    await p.shutdown()


# ---------- apply_patch ----------


def test_apply_unified_diff_basic() -> None:
    orig = "a\nb\nc\n"
    patch = "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    new = _apply_unified_diff(orig, patch)
    assert new == "a\nB\nc\n"


def test_apply_unified_diff_addition() -> None:
    orig = "a\nb\n"
    patch = "@@ -1,2 +1,3 @@\n a\n b\n+c\n"
    new = _apply_unified_diff(orig, patch)
    assert new == "a\nb\nc\n"


def test_apply_unified_diff_with_headers() -> None:
    orig = "a\nb\n"
    patch = "--- a/foo\n+++ b/foo\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
    new = _apply_unified_diff(orig, patch)
    assert new == "a\nB\n"


def test_apply_unified_diff_tolerates_offset() -> None:
    orig = "x\nx\nx\na\nb\nc\n"
    # Wrong line numbers — should re-locate
    patch = "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    new = _apply_unified_diff(orig, patch)
    assert new == "x\nx\nx\na\nB\nc\n"


async def test_apply_patch_tool(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    await sb.write("f.py", "x = 1\ny = 2\n")
    tools = {t.name: t for t in sandbox_tools(sb)}
    patch = "@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 99\n"
    out = await tools["apply_patch"].invoke(
        {"path": "f.py", "patch": patch}, _agent_ctx()
    )
    assert "y = 99" in out or "y = 2" in out  # diff text
    assert (await sb.read("f.py")) == b"x = 1\ny = 99\n"


async def test_apply_patch_new_file(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    tools = {t.name: t for t in sandbox_tools(sb)}
    patch = "@@ -0,0 +1,2 @@\n+hello\n+world\n"
    await tools["apply_patch"].invoke({"path": "new.txt", "patch": patch}, _agent_ctx())
    assert (await sb.read("new.txt")) == b"hello\nworld\n"
