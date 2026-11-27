"""Runner-level integration: Agent(workspace=...) wiring and the shell policy."""

from __future__ import annotations

from dataclasses import replace

import pytest

from lovia import Agent, Runner, events, tool
from lovia.workspace import (
    CommandRule,
    Workspace,
    WorkspacePolicy,
    WorkspaceSessionBinding,
)
from tests.scripted_provider import ScriptedProvider, call, text


def _asks_outside(**kwargs) -> WorkspacePolicy:
    """coding, but asking before reads outside the root (it allows them)."""
    return replace(WorkspacePolicy.coding(**kwargs), read_outside="ask")


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
async def test_background_status_reminder_reaches_the_view(tmp_path) -> None:
    # Turn 1 starts a background process; the turn-2 model view must carry the
    # transient status reminder (wired via WorkspaceLike.view_injectors), and
    # the persisted transcript must not.
    provider = ScriptedProvider(
        [
            call("shell", {"command": "sleep 5", "background": True}, call_id="bg"),
            text("started"),
        ]
    )
    agent = Agent(
        name="coder",
        model=provider,
        workspace=Workspace.local(str(tmp_path), mode="trusted"),
    )

    result = await Runner.run(agent, "start the server")

    marker = "Background processes:"
    turn_two = provider.calls[1]
    assert any(
        m.role == "user" and marker in (m.content or "") and "sleep 5" in m.content
        for m in turn_two
    )
    assert not any(marker in (getattr(e, "content", "") or "") for e in result.entries)
    # The runner-owned session closed at run end, killing the process with it.


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
    # ``printf`` is on no allow list, so under coding it asks (``echo`` is
    # read-only and would run unprompted).
    provider = ScriptedProvider(
        [call("shell", {"command": "printf hi"}, call_id="sh"), text("done")]
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


class _Offers(ScriptedProvider):
    """Records the tool names each call offers."""

    def __init__(self, script, sees: bool | None = None) -> None:
        super().__init__(script)
        if sees is not None:
            self.supports_vision = sees
        self.offered: list[list[str]] = []

    async def stream(self, entries, *, tools=None, **kwargs):
        self.offered.append(sorted(t["function"]["name"] for t in tools or []))
        async for delta in super().stream(entries, tools=tools, **kwargs):
            yield delta


@tool(returns_images=True)
def _shot() -> list:
    """Return an image."""
    from lovia.parts import ImagePart

    return [ImagePart(data="iVBORw0KGgo=", mime_type="image/png")]


@tool(describes_images=True)
def _describe(path: str) -> str:
    """Describe an image."""
    return "a cat"


@pytest.mark.asyncio
async def test_a_call_offers_the_image_tools_for_what_the_model_sees(tmp_path) -> None:
    """returns_images tools while the model sees, describes_images tools while
    it doesn't — wherever they come from, workspace or agent.tools."""
    for sees, expected in (
        (None, ["_shot", "view_image"]),  # undeclared: taken to see
        (False, ["_describe"]),
    ):
        provider = _Offers([text("ok")], sees=sees)
        agent = Agent(
            name="t",
            model=provider,
            tools=[_shot, _describe],
            workspace=Workspace.local(str(tmp_path), mode="readonly"),
        )
        await Runner.run(agent, "go")
        image_tools = [
            n for n in provider.offered[0] if n in {"_shot", "_describe", "view_image"}
        ]
        assert image_tools == expected, sees


@pytest.mark.asyncio
async def test_a_tool_not_offered_still_runs_and_its_image_is_withheld(
    tmp_path,
) -> None:
    from lovia.parts import ImagePart
    from lovia.transcript import InputEntry, ToolResultEntry

    (tmp_path / "s.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    provider = _Offers(
        [call("view_image", {"path": "s.png"}, call_id="v"), text("done")], sees=False
    )
    seen: list = []
    original = provider.stream

    async def stream(entries, **kwargs):
        seen.append(entries)
        async for d in original(entries, **kwargs):
            yield d

    provider.stream = stream  # type: ignore[method-assign]
    agent = Agent(
        name="t",
        model=provider,
        workspace=Workspace.local(str(tmp_path), mode="readonly"),
    )
    result = await Runner.run(agent, "look at s.png")

    entry = next(e for e in result.entries if isinstance(e, ToolResultEntry))
    assert entry.parts is not None  # the transcript keeps the image
    for e in seen[1]:
        parts = (
            e.parts
            if isinstance(e, ToolResultEntry)
            else e.content
            if isinstance(e, InputEntry) and isinstance(e.content, list)
            else None
        )
        assert not any(isinstance(p, ImagePart) for p in parts or [])


@pytest.mark.asyncio
async def test_view_image_offered_to_vision_models(tmp_path) -> None:
    from lovia.transcript import ToolResultEntry

    class VisionScripted(ScriptedProvider):
        supports_vision = True

    (tmp_path / "s.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    # A JPEG saved as .png goes out as what its bytes are; bytes that are no
    # image a provider takes are refused before they reach one.
    (tmp_path / "j.png").write_bytes(b"\xff\xd8\xff\xe0")
    (tmp_path / "x.png").write_bytes(b"abc")
    provider = VisionScripted(
        [
            call("view_image", {"path": "s.png"}, call_id="v"),
            call("view_image", {"path": "j.png"}, call_id="j"),
            call("view_image", {"path": "x.png"}, call_id="x"),
            text("saw it"),
        ]
    )
    agent = Agent(
        name="t",
        model=provider,
        workspace=Workspace.local(str(tmp_path), mode="readonly"),
    )

    result = await Runner.run(agent, "look at s.png")

    png, jpeg, bad = [e for e in result.entries if isinstance(e, ToolResultEntry)]
    assert png.output == "s.png\n[image: image/png, 8 B]"
    assert png.parts is not None and len(png.parts) == 2
    assert jpeg.output == "j.png\n[image: image/jpeg, 4 B]"
    assert bad.is_error and "not PNG, JPEG, GIF or WebP" in bad.output
    assert result.output == "saw it"


@pytest.mark.asyncio
async def test_file_tools_without_workspace_fail_with_hint(tmp_path) -> None:
    from lovia.workspace.tools import read_file

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
async def test_outside_read_asks_then_succeeds_when_approved(tmp_path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    shared = tmp_path / "shared.txt"
    shared.write_text("outside data", encoding="utf-8")

    provider = ScriptedProvider(
        [call("read_file", {"path": str(shared)}, call_id="r"), text("done")]
    )
    agent = Agent(
        name="coder",
        model=provider,
        workspace=Workspace.local(str(root), policy=_asks_outside()),
    )

    handle = Runner.stream(agent, "read the shared file")
    saw_approval = False
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            saw_approval = True
            assert ev.call.name == "read_file"
            ev.approve()

    result = await handle.result()
    assert saw_approval is True
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "outside data" in tool_msg.content


@pytest.mark.asyncio
async def test_outside_read_rejected_reports_denial(tmp_path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    shared = tmp_path / "shared.txt"
    shared.write_text("outside data", encoding="utf-8")

    provider = ScriptedProvider(
        [call("read_file", {"path": str(shared)}, call_id="r"), text("ok")]
    )
    agent = Agent(
        name="coder",
        model=provider,
        workspace=Workspace.local(str(root), policy=_asks_outside()),
    )

    handle = Runner.stream(agent, "read the shared file")
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            ev.reject()

    result = await handle.result()
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "outside data" not in tool_msg.content


@pytest.mark.asyncio
async def test_inside_reads_do_not_ask_under_coding(tmp_path) -> None:
    (tmp_path / "in.txt").write_text("inside", encoding="utf-8")
    provider = ScriptedProvider(
        [call("read_file", {"path": "in.txt"}, call_id="r"), text("done")]
    )
    agent = Agent(
        name="coder",
        model=provider,
        workspace=Workspace.local(str(tmp_path), mode="coding"),
    )

    handle = Runner.stream(agent, "read")
    saw_approval = False
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            saw_approval = True
            ev.approve()

    result = await handle.result()
    assert saw_approval is False
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "inside" in tool_msg.content


@pytest.mark.asyncio
async def test_shell_touching_outside_path_asks_under_coding(tmp_path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "notes.txt"
    outside.write_text("outside notes", encoding="utf-8")

    provider = ScriptedProvider(
        [
            call(
                "shell",
                {"command": f"head -n1 {outside}"},
                call_id="sh",
            ),
            text("done"),
        ]
    )
    agent = Agent(
        name="coder",
        model=provider,
        workspace=Workspace.local(
            str(root),
            policy=_asks_outside(command_rules=(CommandRule("head", "allow"),)),
        ),
    )

    handle = Runner.stream(agent, "peek outside")
    saw_approval = False
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            saw_approval = True  # the path claim escalated an allowed command
            ev.approve()

    result = await handle.result()
    assert saw_approval is True
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "outside notes" in tool_msg.content


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
async def test_a_custom_backend_binds_a_session_across_runs(tmp_path) -> None:
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")
    local = Workspace.local(str(tmp_path), mode="trusted")

    class Backend:
        """A ``WorkspaceLike`` that is not a ``LocalWorkspace``."""

        close_after_run = True
        opened = 0

        async def open(self):
            Backend.opened += 1
            return await local.open()

        tools = staticmethod(local.tools)
        instructions = staticmethod(local.instructions)
        view_injectors = staticmethod(local.view_injectors)

    backend = Backend()
    session = await backend.open()
    binding = WorkspaceSessionBinding(backend, session)
    try:
        for reply in ("first", "second"):
            provider = ScriptedProvider(
                [call("read_file", {"path": "one.txt"}, call_id="r"), text(reply)]
            )
            agent = Agent(name="coder", model=provider, workspace=binding)
            result = await Runner.run(agent, "read")
            assert result.output == reply
        # Both runs used the bound session: the runner neither opened another
        # nor closed this one.
        assert Backend.opened == 1
        assert (await session.read_text("one.txt")).content == "one"
    finally:
        await session.close()


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
