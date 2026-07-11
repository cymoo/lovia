"""Tests for the policy-provided ``recall_tool_result`` tool."""

from __future__ import annotations

from lovia import Agent
from lovia.context.state import result_digest
from lovia.run_context import RunContext
from lovia.tools import make_recall_tool, run_tool

from .helpers import FakeProviderWithWindow, FakeResultStore, call, out, user


def _ctx(entries) -> RunContext:
    agent = Agent(name="t", instructions="x", model=FakeProviderWithWindow())
    return RunContext(context=None, entries=entries, agent=agent)


async def test_recall_falls_back_to_transcript():
    entries = [
        call("c1"),
        out("c1", "the full output"),
        user("hi"),
    ]
    recall = make_recall_tool(None)
    got = await run_tool(recall, {"ref": "c1"}, _ctx(entries))
    assert got == "the full output"


async def test_recall_reads_store_first():
    # The transcript and store disagree; the store wins.
    entries = [call("c1"), out("c1", "stale transcript copy")]
    store = FakeResultStore()
    store.data["c1"] = "fresh store copy"
    recall = make_recall_tool(store)
    got = await run_tool(recall, {"ref": "c1"}, _ctx(entries))
    assert got == "fresh store copy"


async def test_recall_store_miss_falls_back_to_transcript():
    entries = [call("c1"), out("c1", "from transcript")]
    store = FakeResultStore()  # empty: a miss
    recall = make_recall_tool(store)
    got = await run_tool(recall, {"ref": "c1"}, _ctx(entries))
    assert got == "from transcript"


async def test_recall_missing_ref():
    got = await run_tool(make_recall_tool(None), {"ref": "nope"}, _ctx([user("hi")]))
    assert "No tool result found" in got


async def test_recall_store_failure_falls_back_to_transcript():
    # A store read failure must degrade to the transcript (source of truth),
    # not surface as a tool error.
    class _BoomStore:
        async def put(self, key: str, content: str) -> None: ...

        async def get(self, key: str) -> str | None:
            raise RuntimeError("store down")

    entries = [call("c1"), out("c1", "from transcript")]
    got = await run_tool(make_recall_tool(_BoomStore()), {"ref": "c1"}, _ctx(entries))
    assert got == "from transcript"


async def test_recall_by_digest_when_store_is_gone():
    """An offload marker's digest reference must resolve from the transcript
    when the store missed — an ephemeral store lost to a restart, or none."""
    output = "huge offloaded output " * 100
    entries = [call("c1"), out("c1", output)]
    got = await run_tool(
        make_recall_tool(FakeResultStore()),
        {"ref": result_digest(output)},
        _ctx(entries),
    )
    assert got == output


async def test_recall_prefers_call_id_over_digest_scan():
    # A ref that happens to be a call_id resolves by the cheap exact scan;
    # the hash scan is the last resort only.
    output = "content"
    entries = [call("c1"), out("c1", output)]
    got = await run_tool(make_recall_tool(None), {"ref": "c1"}, _ctx(entries))
    assert got == output


async def test_recall_cross_session_isolation_with_shared_store():
    """Two sessions share one policy store and a provider that reuses
    ``call_0``. Content-addressed keys keep each session's recall pointing at
    its own bytes — the regression this design exists to prevent."""
    store = FakeResultStore()
    a_output, b_output = "SESSION A: secret dossier", "SESSION B: harmless log"
    # Each session offloads under its own content key (as the stage does).
    await store.put(result_digest(a_output), a_output)
    await store.put(result_digest(b_output), b_output)

    recall = make_recall_tool(store)
    ctx_a = _ctx([call("call_0"), out("call_0", a_output)])
    ctx_b = _ctx([call("call_0"), out("call_0", b_output)])
    assert await run_tool(recall, {"ref": result_digest(a_output)}, ctx_a) == a_output
    assert await run_tool(recall, {"ref": result_digest(b_output)}, ctx_b) == b_output
