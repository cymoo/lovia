from __future__ import annotations

import pytest

from lovia import Agent, Runner, events, tool
from lovia.sandbox import PathOutsideSandboxError, Sandbox

from tests.scripted_provider import ScriptedProvider, call, text


@pytest.mark.asyncio
async def test_agent_sandbox_injects_tools_and_instructions(tmp_path) -> None:
    (tmp_path / "hello.txt").write_text("hello from sandbox", encoding="utf-8")
    provider = ScriptedProvider(
        [call("read_file", {"path": "hello.txt"}, call_id="read"), text("done")]
    )
    agent = Agent(
        name="coder",
        model=provider,
        instructions="You are a focused coding agent.",
        sandbox=Sandbox.local(str(tmp_path), mode="trusted"),
    )

    result = await Runner.run(agent, "read hello.txt")

    system_message = provider.calls[0][0]
    assert system_message.role == "system"
    assert "You have access to a sandbox" in system_message.content
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "hello from sandbox" in tool_msg.content
    assert result.output == "done"


@pytest.mark.asyncio
async def test_sandbox_tool_conflicts_raise(tmp_path) -> None:
    @tool(name="read_file")
    async def custom_read_file() -> str:
        """Custom conflicting reader."""
        return "custom"

    provider = ScriptedProvider([text("unused")])
    agent = Agent(
        name="coder",
        model=provider,
        tools=[custom_read_file],
        sandbox=Sandbox.local(str(tmp_path)),
    )

    with pytest.raises(Exception, match="Tool name conflict"):
        await Runner.run(agent, "go")


@pytest.mark.asyncio
async def test_coding_mode_shell_uses_existing_approval_flow(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            call(
                "shell", {"command": "echo hi", "reason": "verify shell"}, call_id="sh"
            ),
            text("done"),
        ]
    )
    agent = Agent(name="coder", model=provider, sandbox=Sandbox.local(str(tmp_path)))

    handle = Runner.stream(agent, "run shell")
    saw_approval = False
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            saw_approval = True
            ev.approve()

    result = await handle.result()
    assert saw_approval is True
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert '"stdout": "hi\\n"' in tool_msg.content


@pytest.mark.asyncio
async def test_readonly_mode_does_not_expose_shell(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("shell", {"command": "echo hi"}, call_id="sh"), text("done")]
    )
    agent = Agent(
        name="reviewer",
        model=provider,
        sandbox=Sandbox.local(str(tmp_path), mode="readonly"),
    )

    result = await Runner.run(agent, "run shell")

    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "not available" in tool_msg.content


@pytest.mark.asyncio
async def test_explicit_session_is_not_closed_by_runner(tmp_path) -> None:
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")

    async with Sandbox.local(str(tmp_path), mode="trusted").session() as sandbox:
        provider = ScriptedProvider(
            [call("read_file", {"path": "one.txt"}, call_id="r1"), text("first")]
        )
        agent = Agent(name="coder", model=provider, sandbox=sandbox)
        await Runner.run(agent, "read")

        content = await sandbox.read_text("one.txt")
        assert content.content == "one"


@pytest.mark.asyncio
async def test_local_sandbox_rejects_absolute_and_escape_paths(tmp_path) -> None:
    sandbox = Sandbox.local(str(tmp_path), mode="trusted")
    session = await sandbox.open()
    try:
        with pytest.raises(PathOutsideSandboxError):
            await session.read_text("/tmp/nope")
        with pytest.raises(PathOutsideSandboxError):
            await session.read_text("../nope")
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_local_sandbox_rejects_symlink_escape(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    session = await Sandbox.local(str(tmp_path)).open()
    try:
        with pytest.raises(PathOutsideSandboxError):
            await session.read_text("link.txt")
    finally:
        await session.close()
