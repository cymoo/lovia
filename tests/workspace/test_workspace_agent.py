"""Runner-level integration: Agent(workspace=...) wiring and the shell policy."""

from __future__ import annotations

import pytest

from lovia import Agent, Runner, events, tool
from lovia.workspace import CommandRule, Workspace

from tests.scripted_provider import ScriptedProvider, call, text


@pytest.mark.asyncio
async def test_workspace_injects_tools_and_instructions(tmp_path) -> None:
    (tmp_path / "hello.txt").write_text("hello from workspace", encoding="utf-8")
    provider = ScriptedProvider(
        [call("read_file", {"path": "hello.txt"}, call_id="read"), text("done")]
    )
    agent = Agent(
        name="coder",
        model=provider,
        instructions="You are a focused coding agent.",
        workspace=Workspace.local(str(tmp_path), mode="trusted"),
    )

    result = await Runner.run(agent, "read hello.txt")

    system_message = provider.calls[0][0]
    assert system_message.role == "system"
    assert "## Workspace" in system_message.content
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "hello from workspace" in tool_msg.content
    assert result.output == "done"


@pytest.mark.asyncio
async def test_full_file_tool_round_trip(tmp_path) -> None:
    (tmp_path / "app.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    provider = ScriptedProvider(
        [
            call("grep_files", {"pattern": "return"}, call_id="g"),
            call(
                "edit_file",
                {"path": "app.py", "old": "return 1", "new": "return 2"},
                call_id="e",
            ),
            call("write_file", {"path": "new.txt", "content": "note"}, call_id="w"),
            call("list_files", {"pattern": "**/*"}, call_id="l"),
            text("all done"),
        ]
    )
    agent = Agent(
        name="coder",
        model=provider,
        workspace=Workspace.local(str(tmp_path), mode="trusted"),
    )

    result = await Runner.run(agent, "refactor")

    tool_outputs = [m.content for m in result.messages if m.role == "tool"]
    assert any("app.py:2" in out for out in tool_outputs)  # grep renderer
    assert (tmp_path / "app.py").read_text() == "def foo():\n    return 2\n"
    assert (tmp_path / "new.txt").read_text() == "note"
    assert any("new.txt" in out and "app.py" in out for out in tool_outputs)
    assert result.output == "all done"


@pytest.mark.asyncio
async def test_shell_ask_goes_through_approval(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("shell", {"command": "echo hi"}, call_id="sh"), text("done")]
    )
    agent = Agent(
        name="coder",
        model=provider,
        workspace=Workspace.local(str(tmp_path), mode="coding"),
    )

    handle = Runner.stream(agent, "run shell")
    saw_approval = False
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            saw_approval = True
            ev.approve()

    result = await handle.result()
    assert saw_approval is True
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "hi" in tool_msg.content


@pytest.mark.asyncio
async def test_shell_allow_rule_skips_approval(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("shell", {"command": "echo ok"}, call_id="sh"), text("done")]
    )
    agent = Agent(
        name="coder",
        model=provider,
        workspace=Workspace.local(
            str(tmp_path), mode="coding", command_rules=(CommandRule("echo", "allow"),)
        ),
    )

    saw_approval = False
    handle = Runner.stream(agent, "run shell")
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            saw_approval = True
            ev.approve()

    result = await handle.result()
    assert saw_approval is False
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "ok" in tool_msg.content


@pytest.mark.asyncio
async def test_shell_deny_rule_reports_error_to_model(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("shell", {"command": "rm -rf ."}, call_id="sh"), text("understood")]
    )
    agent = Agent(
        name="coder",
        model=provider,
        workspace=Workspace.local(
            str(tmp_path), mode="trusted", command_rules=(CommandRule("rm", "deny"),)
        ),
    )

    result = await Runner.run(agent, "clean up")

    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "denied by workspace policy" in tool_msg.content
    assert result.output == "understood"


@pytest.mark.asyncio
async def test_readonly_workspace_exposes_no_shell_or_write(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("shell", {"command": "echo hi"}, call_id="sh"), text("done")]
    )
    agent = Agent(
        name="reviewer",
        model=provider,
        workspace=Workspace.local(str(tmp_path), mode="readonly"),
    )

    result = await Runner.run(agent, "run shell")

    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "not available" in tool_msg.content


@pytest.mark.asyncio
async def test_file_tools_without_workspace_fail_with_hint(tmp_path) -> None:
    from lovia.tools import read_file

    provider = ScriptedProvider(
        [call("read_file", {"path": "x.txt"}, call_id="r"), text("done")]
    )
    agent = Agent(name="bare", model=provider, tools=[read_file])

    result = await Runner.run(agent, "read")

    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "No workspace is configured" in tool_msg.content


@pytest.mark.asyncio
async def test_workspace_tool_conflicts_raise(tmp_path) -> None:
    @tool(name="read_file")
    async def custom_read_file() -> str:
        """Custom conflicting reader."""
        return "custom"

    provider = ScriptedProvider([text("unused")])
    agent = Agent(
        name="coder",
        model=provider,
        tools=[custom_read_file],
        workspace=Workspace.local(str(tmp_path)),
    )

    with pytest.raises(Exception, match="Tool name conflict"):
        await Runner.run(agent, "go")


@pytest.mark.asyncio
async def test_user_owned_session_survives_runs(tmp_path) -> None:
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")

    async with Workspace.local(str(tmp_path), mode="trusted").session() as binding:
        provider = ScriptedProvider(
            [call("read_file", {"path": "one.txt"}, call_id="r1"), text("first")]
        )
        agent = Agent(name="coder", model=provider, workspace=binding)
        await Runner.run(agent, "read")

        # The session is still usable after the run finished.
        session = await binding.open()
        content = await session.read_text("one.txt")
        assert content.content == "one"


@pytest.mark.asyncio
async def test_handoff_swaps_workspace_session(tmp_path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "who.txt").write_text("agent A", encoding="utf-8")
    (root_b / "who.txt").write_text("agent B", encoding="utf-8")

    second = Agent(
        name="second",
        model=ScriptedProvider(
            [call("read_file", {"path": "who.txt"}, call_id="r2"), text("done")]
        ),
        workspace=Workspace.local(str(root_b), mode="trusted"),
    )
    first = Agent(
        name="first",
        model=ScriptedProvider([call("transfer_to_second", {}, call_id="t")]),
        workspace=Workspace.local(str(root_a), mode="trusted"),
        handoffs=[second],
    )

    result = await Runner.run(first, "go")

    tool_outputs = [m.content for m in result.messages if m.role == "tool"]
    assert any("agent B" in out for out in tool_outputs)
    assert not any("agent A" in out for out in tool_outputs)
