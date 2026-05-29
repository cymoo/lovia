from __future__ import annotations

import os
from pathlib import Path

import pytest

from lovia import Agent
from lovia.run_context import RunContext
from lovia.workspace import (
    ExecLimits,
    LocalWorkspace,
    PathEscape,
    Workspace,
    bash,
    edit_file,
    glob,
    list_dir,
    read_file,
    write_file,
)
from lovia.workspace.workspace import default_workspace


def _ctx() -> RunContext[None]:
    return RunContext(context=None, messages=[], agent=Agent(name="test"))


@pytest.mark.asyncio
async def test_workspace_file_tools_and_ranges(tmp_path: Path) -> None:
    ws = Workspace(root=tmp_path)

    assert await ws.write_file("src/app.py", "a\nb\nc\n") == 6
    assert (
        await ws.read_file("/workspace/src/app.py", start_line=2, max_lines=1) == "b\n"
    )
    assert await ws.edit_file("src/app.py", "b\n", "B\n") == 1
    assert await ws.read_file("src/app.py") == "a\nB\nc\n"

    with pytest.raises(Exception, match="already exists"):
        await ws.write_file("src/app.py", "x", overwrite=False)

    with pytest.raises(Exception, match="0 matches"):
        await ws.edit_file("src/app.py", "missing", "x")


@pytest.mark.asyncio
async def test_workspace_rejects_path_escape(tmp_path: Path) -> None:
    ws = Workspace(root=tmp_path)

    with pytest.raises(PathEscape):
        await ws.read_file("../outside.txt")

    with pytest.raises(PathEscape):
        await ws.read_file("/etc/passwd")


@pytest.mark.asyncio
async def test_glob_and_list_dir_defaults_skip_hidden(tmp_path: Path) -> None:
    ws = Workspace(root=tmp_path)
    await ws.write_file("a.txt", "a")
    await ws.write_file(".secret", "s")
    await ws.write_file("pkg/__init__.py", "")
    await ws.write_file("pkg/.hidden.py", "")

    assert await ws.glob("**/*") == ["a.txt", "pkg", "pkg/__init__.py"]
    assert ".secret" not in [entry.name for entry in await ws.list_dir(".")]
    assert ".secret" in [
        entry.name for entry in await ws.list_dir(".", include_hidden=True)
    ]


@pytest.mark.asyncio
async def test_bash_returns_structured_result_and_timeout(tmp_path: Path) -> None:
    ws = Workspace(root=tmp_path)

    result = await ws.run("printf hi")
    assert result.exit_code == 0
    assert result.stdout == "hi"
    assert result.stderr == ""
    assert not result.timed_out

    timed_out = await ws.run(
        "sleep 2",
        limits=ExecLimits(timeout=0.1, max_output_bytes=1_000),
    )
    assert timed_out.exit_code is None
    assert timed_out.timed_out


@pytest.mark.asyncio
async def test_bash_tool_shape(tmp_path: Path) -> None:
    ws = Workspace(root=tmp_path)
    tool = bash(ws, audit=None)

    result = await tool.invoke({"command": "printf ok"}, _ctx())

    assert result == {
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
        "timed_out": False,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_individual_tool_factories(tmp_path: Path) -> None:
    ws = Workspace(root=tmp_path)
    ctx = _ctx()

    assert write_file(ws).name == "write_file"
    assert await write_file(ws).invoke({"path": "hello.txt", "content": "hello"}, ctx)
    assert await read_file(ws).invoke({"path": "hello.txt"}, ctx) == "hello"
    assert await edit_file(ws).invoke(
        {"path": "hello.txt", "old_text": "hell", "new_text": "yell"}, ctx
    ) == {"replacements": 1}
    assert await read_file(ws).invoke({"path": "hello.txt"}, ctx) == "yello"
    assert glob(ws).name == "glob"
    assert await glob(ws).invoke({"pattern": "*.txt"}, ctx) == ["hello.txt"]
    assert list_dir(ws).name == "list_dir"
    assert [entry["name"] for entry in await list_dir(ws).invoke({}, ctx)] == [
        "hello.txt"
    ]


@pytest.mark.asyncio
async def test_adaptive_python_venv_keeps_host_home(tmp_path: Path) -> None:
    ws = LocalWorkspace(root=tmp_path)

    result = await ws.exec(
        "python - <<'PY'\nimport os, sys\nprint(sys.prefix)\nprint(os.environ.get('HOME'))\nPY"
    )

    assert result.exit_code == 0
    prefix, home = result.stdout.strip().splitlines()
    assert "/lovia/workspace-python/" in prefix
    assert prefix.endswith("/python")
    assert not prefix.startswith(str(tmp_path))
    assert home == os.environ.get("HOME")


@pytest.mark.asyncio
async def test_adaptive_python_ignores_repo_local_lovia_bin(tmp_path: Path) -> None:
    fake_bin = tmp_path / ".lovia" / "python" / "bin"
    fake_bin.mkdir(parents=True)
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\nprintf hacked\n", encoding="utf-8")
    fake_python.chmod(0o755)

    ws = LocalWorkspace(root=tmp_path)
    result = await ws.exec("python - <<'PY'\nprint('safe')\nPY")

    assert result.exit_code == 0
    assert result.stdout.strip() == "safe"


def test_default_workspace_is_cwd_keyed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert default_workspace() is default_workspace()
