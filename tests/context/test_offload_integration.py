"""End-to-end offload: runner + real LocalWorkspace + pipeline."""

from __future__ import annotations

from lovia import Agent, Runner
from lovia.context import Compaction, OffloadToolResults
from lovia.events import ContextCompacted
from lovia.run_context import RunContext
from lovia.tools import recall_tool_result, run_tool
from lovia.transcript import ToolResultEntry
from lovia.workspace import Workspace

from ..scripted_provider import ScriptedProvider, text
from .helpers import call, out

BIG = "alpha beta " * 800  # ~8.8K chars ≈ 2.2K estimated tokens


async def test_offload_archives_old_result_and_view_carries_marker(tmp_path):
    provider = ScriptedProvider([text("done")])
    agent = Agent(
        name="t",
        instructions="x",
        model=provider,
        workspace=Workspace.local(str(tmp_path)),
    )
    # Two earlier big tool results in the session history; the older one
    # should be archived, the newer one kept verbatim (keep_last=1).
    from lovia import InMemorySession

    sess = InMemorySession()
    await sess.append("s1", [call("c1"), out("c1", BIG), call("c2"), out("c2", BIG)])
    pipeline = Compaction(
        context_window=2_500,
        reserve_output_tokens=0,
        stages=[OffloadToolResults(min_chars=1_000, keep_last=1)],
    )

    events_seen: list = []
    async for ev in Runner.stream(
        agent,
        "summarize the data",
        context_policy=pipeline,
        session=sess,
        session_id="s1",
    ):
        events_seen.append(ev)

    # The full output landed in a workspace file.
    archived = tmp_path / ".context" / "tool-c1.txt"
    assert archived.read_text() == BIG

    # The provider saw the marker for c1 and the full output for c2.
    tool_messages = {
        m.tool_call_id: m.content for m in provider.calls[0] if m.role == "tool"
    }
    assert "archived to workspace file: .context/tool-c1.txt" in tool_messages["c1"]
    assert "alpha beta" in tool_messages["c1"]  # preview included
    assert tool_messages["c2"] == BIG

    compacted = [e for e in events_seen if isinstance(e, ContextCompacted)]
    assert len(compacted) == 1
    assert compacted[0].reason == "offload"

    # The session still holds the untouched output.
    persisted = await sess.load("s1")
    full = [
        e for e in persisted if isinstance(e, ToolResultEntry) and e.call_id == "c1"
    ]
    assert full and full[0].output == BIG

    # And recall_tool_result can still fetch it from the transcript.
    ctx = RunContext(context=None, entries=persisted, agent=agent)
    assert await run_tool(recall_tool_result, {"call_id": "c1"}, ctx) == BIG
