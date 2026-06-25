"""Tests for the policy-provided ``recall_tool_result`` tool."""

from __future__ import annotations

from lovia import Agent
from lovia.run_context import RunContext
from lovia.tools import make_recall_tool, run_tool

from .helpers import FakeProviderWithWindow, FakeResultStore, call, out, user


async def test_recall_falls_back_to_transcript():
    entries = [
        call("c1"),
        out("c1", "the full output"),
        user("hi"),
    ]
    agent = Agent(name="t", instructions="x", model=FakeProviderWithWindow())
    ctx = RunContext(context=None, entries=entries, agent=agent)
    recall = make_recall_tool(None)
    got = await run_tool(recall, {"call_id": "c1"}, ctx)
    assert got == "the full output"


async def test_recall_reads_store_first():
    # The transcript and store disagree; the store wins.
    entries = [call("c1"), out("c1", "stale transcript copy")]
    store = FakeResultStore()
    store.data["c1"] = "fresh store copy"
    agent = Agent(name="t", instructions="x", model=FakeProviderWithWindow())
    ctx = RunContext(context=None, entries=entries, agent=agent)
    recall = make_recall_tool(store)
    got = await run_tool(recall, {"call_id": "c1"}, ctx)
    assert got == "fresh store copy"


async def test_recall_store_miss_falls_back_to_transcript():
    entries = [call("c1"), out("c1", "from transcript")]
    store = FakeResultStore()  # empty: a miss
    agent = Agent(name="t", instructions="x", model=FakeProviderWithWindow())
    ctx = RunContext(context=None, entries=entries, agent=agent)
    recall = make_recall_tool(store)
    got = await run_tool(recall, {"call_id": "c1"}, ctx)
    assert got == "from transcript"


async def test_recall_missing_call_id():
    agent = Agent(name="t", instructions="x", model=FakeProviderWithWindow())
    ctx = RunContext(context=None, entries=[user("hi")], agent=agent)
    got = await run_tool(make_recall_tool(None), {"call_id": "nope"}, ctx)
    assert "No tool result found" in got
