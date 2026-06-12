"""Tests for the opt-in ``recall_tool_result`` tool."""

from __future__ import annotations

from lovia import Agent
from lovia.run_context import RunContext
from lovia.tools import recall_tool_result, run_tool

from .helpers import FakeProviderWithWindow, call, out, user


async def test_recall_tool_result_returns_full_output():
    entries = [
        call("c1"),
        out("c1", "the full output"),
        user("hi"),
    ]
    agent = Agent(name="t", instructions="x", model=FakeProviderWithWindow())
    ctx = RunContext(context=None, entries=entries, agent=agent)
    got = await run_tool(recall_tool_result, {"call_id": "c1"}, ctx)
    assert got == "the full output"


async def test_recall_tool_result_missing_call_id():
    agent = Agent(name="t", instructions="x", model=FakeProviderWithWindow())
    ctx = RunContext(context=None, entries=[user("hi")], agent=agent)
    got = await run_tool(recall_tool_result, {"call_id": "nope"}, ctx)
    assert "No tool result found" in got
