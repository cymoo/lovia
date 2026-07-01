"""End-to-end offload: runner + policy-owned result store + pipeline."""

from __future__ import annotations

from lovia import Agent, InMemorySession, Runner
from lovia.context import Compaction, InMemoryResultStore, OffloadToolResults
from lovia.events import ContextCompacted
from lovia.run_context import RunContext
from lovia.tools import make_recall_tool, run_tool
from lovia.transcript import ToolResultEntry

from ..scripted_provider import ScriptedProvider, call as scripted_call, text
from .helpers import call, out

BIG = "alpha beta " * 800  # ~8.8K chars ≈ 2.2K estimated tokens


async def test_offload_archives_old_result_and_view_carries_marker():
    provider = ScriptedProvider([text("done")])
    agent = Agent(name="t", instructions="x", model=provider)
    # Two earlier big tool results in the session history; the older one
    # should be archived, the newer one kept verbatim (keep_last=1).
    sess = InMemorySession()
    await sess.append("s1", [call("c1"), out("c1", BIG), call("c2"), out("c2", BIG)])
    store = InMemoryResultStore()
    pipeline = Compaction(
        context_window=2_500,
        reserve_output_tokens=0,
        store=store,
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

    # The full output landed in the result store.
    assert await store.get("c1") == BIG

    # The provider saw the marker for c1 (preview, no file path) and the full
    # output for c2.
    tool_messages = {
        m.tool_call_id: m.content for m in provider.calls[0] if m.role == "tool"
    }
    assert "trimmed to a preview to save context" in tool_messages["c1"]
    assert "alpha beta" in tool_messages["c1"]  # preview included
    assert 'recall_tool_result("c1")' in tool_messages["c1"]
    assert tool_messages["c2"] == BIG

    compacted = [e for e in events_seen if isinstance(e, ContextCompacted)]
    assert len(compacted) == 1
    assert compacted[0].notice.reason == "offload"

    # The session still holds the untouched output today; the store is the
    # durable copy layered on top (for when the transcript no longer retains it).
    persisted = await sess.load("s1")
    full = [
        e for e in persisted if isinstance(e, ToolResultEntry) and e.call_id == "c1"
    ]
    assert full and full[0].output == BIG

    # recall fetches from the store first...
    ctx = RunContext(context=None, entries=persisted, agent=agent)
    assert await run_tool(make_recall_tool(store), {"call_id": "c1"}, ctx) == BIG
    # ...and falls back to the transcript when the store has nothing.
    assert await run_tool(make_recall_tool(None), {"call_id": "c1"}, ctx) == BIG


async def test_runner_dispatches_policy_provided_recall_tool():
    """The compacting policy injects recall_tool_result with no manual wiring;
    the model can call it and the runner actually dispatches it end-to-end."""
    sess = InMemorySession()
    await sess.append("s1", [call("c1"), out("c1", BIG)])
    store = InMemoryResultStore()
    policy = Compaction(
        context_window=2_500,
        reserve_output_tokens=0,
        store=store,
        stages=[OffloadToolResults(min_chars=1_000, keep_last=0)],
    )
    # Turn 1: the model calls recall for the dropped result; turn 2: it answers.
    provider = ScriptedProvider(
        [scripted_call("recall_tool_result", {"call_id": "c1"}), text("done")]
    )
    agent = Agent(name="t", instructions="x", model=provider)  # no tools added

    result = await Runner.run(
        agent, "what was c1?", context_policy=policy, session=sess, session_id="s1"
    )
    assert result.output == "done"
    # The recall tool was registered (by the policy) and dispatched: its result
    # is the full output, not a "tool not found" error.
    persisted = await sess.load("s1")
    recalled = [
        e
        for e in persisted
        if isinstance(e, ToolResultEntry) and e.call_id == "call_recall_tool_result"
    ]
    assert recalled and recalled[0].output == BIG


async def test_offload_without_store_markers_and_recall_falls_back():
    """Storeless offload — the default ``Compaction(context_window=...)`` config
    the runtime builds — still replaces big results with a preview marker, and
    recall recovers the full output from the transcript (no store needed)."""
    provider = ScriptedProvider([text("done")])
    agent = Agent(name="t", instructions="x", model=provider)
    sess = InMemorySession()
    await sess.append("s1", [call("c1"), out("c1", BIG), call("c2"), out("c2", BIG)])
    # No store passed — exactly what loop.py / web/app.py construct by default.
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

    # Offload ran without a store: the older big result became a preview marker;
    # the newest stayed verbatim (keep_last=1).
    tool_messages = {
        m.tool_call_id: m.content for m in provider.calls[0] if m.role == "tool"
    }
    assert "trimmed to a preview to save context" in tool_messages["c1"]
    assert "alpha beta" in tool_messages["c1"]  # preview kept inline
    assert 'recall_tool_result("c1")' in tool_messages["c1"]
    assert tool_messages["c2"] == BIG

    # The stage still fired (the pipeline reports it)...
    compacted = [e for e in events_seen if isinstance(e, ContextCompacted)]
    assert len(compacted) == 1 and compacted[0].notice.reason == "offload"

    # ...and although nothing was archived, recall recovers c1 from the
    # transcript via the entries-only fallback (store=None).
    persisted = await sess.load("s1")
    ctx = RunContext(context=None, entries=persisted, agent=agent)
    assert await run_tool(make_recall_tool(None), {"call_id": "c1"}, ctx) == BIG
