from __future__ import annotations

import json

import pytest

from lovia import Agent, RunContext
from lovia.sandbox import PathOutsideSandboxError
from lovia.tools import coding_tools
from lovia.tools.read_file import read_file

from tests.scripted_provider import ScriptedProvider


def _ctx() -> RunContext[None]:
    return RunContext(
        context=None,
        messages=[],
        agent=Agent(name="test", model=ScriptedProvider([])),
    )


async def _invoke(tools, name: str, args: dict[str, object]) -> object:
    return await {t.name: t for t in tools}[name].invoke(args, _ctx())


@pytest.mark.asyncio
async def test_coding_tools_read_write_and_edit(tmp_path) -> None:
    tools = coding_tools(root=str(tmp_path), mode="trusted")

    created = await _invoke(
        tools, "write_file", {"path": "pkg/app.py", "content": "print('old')\n"}
    )
    assert created.action == "created"

    edited = await _invoke(
        tools,
        "edit_file",
        {"path": "pkg/app.py", "old": "old", "new": "new"},
    )
    assert edited.ok is True
    assert edited.changed is True

    read = await _invoke(tools, "read_file", {"path": "pkg/app.py"})
    assert "print('new')" in read.content


@pytest.mark.asyncio
async def test_edit_file_returns_recoverable_failures(tmp_path) -> None:
    (tmp_path / "app.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    tools = coding_tools(root=str(tmp_path), mode="trusted")

    missing = await _invoke(
        tools, "edit_file", {"path": "app.py", "old": "y = 1", "new": "y = 2"}
    )
    assert missing.ok is False
    assert "not found" in missing.message

    multiple = await _invoke(
        tools, "edit_file", {"path": "app.py", "old": "x = 1", "new": "x = 2"}
    )
    assert multiple.ok is False
    assert multiple.replacements == 2
    assert "multiple" in multiple.message


@pytest.mark.asyncio
async def test_write_file_create_only_prevents_overwrite(tmp_path) -> None:
    (tmp_path / "app.py").write_text("old", encoding="utf-8")
    tools = coding_tools(root=str(tmp_path), mode="trusted")

    result = await _invoke(
        tools,
        "write_file",
        {"path": "app.py", "content": "new", "create_only": True},
    )

    assert result.ok is False
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "old"


@pytest.mark.asyncio
async def test_read_file_reports_truncation_and_ranges(tmp_path) -> None:
    (tmp_path / "big.txt").write_text(
        "\n".join(str(i) for i in range(100)), encoding="utf-8"
    )
    tools = coding_tools(root=str(tmp_path), mode="trusted")

    result = await _invoke(
        tools, "read_file", {"path": "big.txt", "start": 10, "end": 12}
    )

    assert result.start == 10
    assert result.end == 12
    assert result.total_lines == 100
    assert result.truncated is True


@pytest.mark.asyncio
async def test_shell_enforces_relative_cwd_and_returns_structured_result(
    tmp_path,
) -> None:
    (tmp_path / "pkg").mkdir()
    tools = coding_tools(root=str(tmp_path), mode="trusted")

    ok = await _invoke(tools, "shell", {"command": "pwd", "cwd": "pkg"})
    assert ok.exit_code == 0
    assert ok.stdout.rstrip().endswith("/pkg")

    with pytest.raises(PathOutsideSandboxError):
        await _invoke(tools, "shell", {"command": "echo nope", "cwd": "../"})


@pytest.mark.asyncio
async def test_shell_timeout_returns_timed_out(tmp_path) -> None:
    tools = coding_tools(root=str(tmp_path), mode="trusted")

    result = await _invoke(
        tools,
        "shell",
        {"command": "python -c 'import time; time.sleep(1)'", "timeout": 0.01},
    )

    assert result.timed_out is True


def test_tool_results_render_as_json_for_models(tmp_path) -> None:
    tools = coding_tools(root=str(tmp_path), mode="trusted")
    schemas = {t.name: t.openai_schema() for t in tools}
    assert "read_file" in schemas
    json.dumps(schemas)


def test_split_tool_modules_keep_factory_exports(tmp_path) -> None:
    import importlib

    import lovia.tools

    read_file_module = importlib.import_module("lovia.tools.read_file")

    assert callable(read_file)
    assert callable(read_file_module.read_file)
    assert callable(lovia.tools.read_file)
    assert lovia.tools.read_file(root=str(tmp_path)).name == "read_file"
