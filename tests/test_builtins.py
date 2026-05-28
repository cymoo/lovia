"""Tests for the lovia.builtins.* submodules."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

from lovia.builtins import code, fs, http, search, shell, think, time as time_b, todo
from lovia.builtins.human import HumanChannel, ask_human
from lovia.exceptions import ToolError, UserError
from lovia.run_context import RunContext


def _ctx() -> RunContext:
    return RunContext(context=None, messages=[], agent=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------- http


def test_http_tool_metadata() -> None:
    assert http.http_fetch.name == "http_fetch"
    assert "url" in http.http_fetch.parameters["properties"]


# ---------------------------------------------------------------- time


@pytest.mark.asyncio
async def test_now_returns_iso() -> None:
    result = await time_b.now.invoke({}, _ctx())
    assert "T" in result and (result.endswith("+00:00") or "+" in result or "-" in result[10:])


@pytest.mark.asyncio
async def test_sleep_is_capped() -> None:
    out = await time_b.sleep.invoke({"seconds": 0.01}, _ctx())
    assert "slept" in out


# ---------------------------------------------------------------- think


@pytest.mark.asyncio
async def test_think_echoes() -> None:
    assert await think.think.invoke({"thought": "x"}, _ctx()) == "x"


# ---------------------------------------------------------------- fs


@pytest.mark.asyncio
async def test_fs_read_and_sandbox() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.txt").write_text("hello")
        f = fs.FileSystem(root=tmp, writable=False)
        tools = {t.name: t for t in f.tools()}
        assert "write_file" not in tools
        assert "hello" == await tools["read_file"].invoke({"path": "a.txt"}, _ctx())

        with pytest.raises(ToolError):
            await tools["read_file"].invoke({"path": "../etc/passwd"}, _ctx())


@pytest.mark.asyncio
async def test_fs_write_when_writable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = fs.FileSystem(root=tmp, writable=True)
        tools = {t.name: t for t in f.tools()}
        await tools["write_file"].invoke({"path": "out.txt", "content": "hi"}, _ctx())
        assert (Path(tmp) / "out.txt").read_text() == "hi"


def test_fs_missing_root_raises_user_error() -> None:
    with pytest.raises(UserError):
        fs.FileSystem(root="/nonexistent/lovia/path/_x")


# ---------------------------------------------------------------- shell


@pytest.mark.asyncio
async def test_shell_runs_safe_command() -> None:
    sh = shell.Shell(needs_approval=False, timeout=5)
    out = await sh.tool().invoke({"cmd": "echo hi"}, _ctx())
    assert out["exit_code"] == 0
    assert "hi" in out["stdout"]


def test_shell_default_needs_approval() -> None:
    sh = shell.Shell()
    assert sh.tool().needs_approval is True


def test_shell_allowlist_predicate() -> None:
    pred = shell.allowlist(["ls", "echo"])
    assert pred({"cmd": "echo hi"}, _ctx()) is False  # allowed
    assert pred({"cmd": "rm -rf /"}, _ctx()) is True  # needs approval


# ---------------------------------------------------------------- code


@pytest.mark.asyncio
async def test_python_runner_executes() -> None:
    runner = code.PythonRunner(needs_approval=False, timeout=10)
    out = await runner.tool().invoke({"code": "print(2 + 2)"}, _ctx())
    assert out["exit_code"] == 0
    assert "4" in out["stdout"]


# ---------------------------------------------------------------- todo


@pytest.mark.asyncio
async def test_todo_lifecycle() -> None:
    todos = todo.TodoList()
    tools = {t.name: t for t in todo.todo_tools(todos)}
    tid = await tools["add_todo"].invoke({"title": "do it"}, _ctx())
    await tools["update_todo"].invoke({"id": tid, "status": "done"}, _ctx())
    rendered = await tools["list_todos"].invoke({}, _ctx())
    assert "[x]" in rendered and "do it" in rendered


# ---------------------------------------------------------------- search


@pytest.mark.asyncio
async def test_web_search_with_custom_backend() -> None:
    class Stub:
        async def search(self, query: str, *, max_results: int = 5):  # type: ignore[no-untyped-def]
            return [search.SearchResult(title="t", url="https://x", snippet="s")]

    s = search.web_search(Stub())
    out = await s.invoke({"query": "x"}, _ctx())
    assert out == [{"title": "t", "url": "https://x", "snippet": "s"}]


def test_duckduckgo_friendly_error_without_dep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the import to fail by clobbering the module name.
    if "duckduckgo_search" in sys.modules:
        monkeypatch.delitem(sys.modules, "duckduckgo_search")
    monkeypatch.setattr(
        "builtins.__import__",
        lambda name, *a, **k: (_ for _ in ()).throw(ImportError(name))
        if name == "duckduckgo_search"
        else __import__(name, *a, **k),
    )
    ddg = search.DuckDuckGoSearch()
    with pytest.raises(UserError) as exc_info:
        asyncio.run(ddg.search("x"))
    assert "lovia[tools]" in str(exc_info.value)


# ---------------------------------------------------------------- human


@pytest.mark.asyncio
async def test_ask_human_resolves_via_channel() -> None:
    channel = HumanChannel()
    tool_ = ask_human(channel)

    async def answerer() -> None:
        await asyncio.sleep(0.01)
        pending = channel.pending
        assert pending
        channel.answer(pending[0].id, "42")

    t = asyncio.create_task(answerer())
    result = await tool_.invoke({"question": "what is the answer?"}, _ctx())
    await t
    assert result == "42"
