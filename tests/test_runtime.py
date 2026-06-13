"""Regression tests for the lovia.runtime rewrite."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from lovia import Agent, Runner, tool
from lovia.exceptions import BudgetExceeded, ContextOverflowError
from lovia.messages import AssistantTurn, ToolCall, Usage
from lovia.reliability import RunBudget
from lovia.stores import InMemoryCheckpointer, InMemorySession
from lovia.context import CompactionRequest, ContextResult
from lovia.transcript import (
    AssistantTextEntry,
    InputEntry,
    TextDelta,
    entries_to_messages,
)

from .scripted_provider import ScriptedProvider, call, text


@tool
async def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
async def ping() -> str:
    """Reply with pong."""
    return "pong"


# ---------------------------------------------------------------------------
# Handoff connects the target agent's MCP servers
# ---------------------------------------------------------------------------


class _FakeMCPServer:
    """Minimal MCPServerLike: acts as its own connection."""

    close_after_run = True

    def __init__(self, *tools: Any) -> None:
        self._tools = list(tools)
        self.opened = 0
        self.closed = 0

    async def open(self) -> "_FakeMCPServer":
        self.opened += 1
        return self

    def tools(self) -> list[Any]:
        return list(self._tools)

    async def close(self) -> None:
        self.closed += 1


async def test_handoff_connects_target_agent_mcp_tools() -> None:
    server = _FakeMCPServer(ping)
    spanish = Agent(
        name="Spanish",
        model=ScriptedProvider(
            [call("ping", {}, call_id="p1"), text("¡pong recibido!")]
        ),
        mcp_servers=[server],
    )
    english = Agent(
        name="English",
        model=ScriptedProvider([call("transfer_to_spanish", {"reason": "idioma"})]),
        handoffs=[spanish],
    )

    result = await Runner.run(english, "Hola")

    assert result.output == "¡pong recibido!"
    assert server.opened == 1
    tool_outputs = [m.content for m in result.messages if m.role == "tool"]
    assert "pong" in tool_outputs
    # Run-scoped connections are closed when the run ends.
    assert server.closed == 1


# ---------------------------------------------------------------------------
# Malformed tool arguments are reported to the model, not swallowed
# ---------------------------------------------------------------------------


async def test_invalid_tool_arguments_reported_to_model() -> None:
    bad_call = AssistantTurn(
        content=None,
        tool_calls=[ToolCall(id="c1", name="add", arguments="{not json")],
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    provider = ScriptedProvider([bad_call, text("let me retry")])
    agent = Agent(name="t", model=provider, tools=[add])

    result = await Runner.run(agent, "go")

    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "Invalid JSON in tool arguments" in tool_msg.content
    assert result.output == "let me retry"


# ---------------------------------------------------------------------------
# Compacted views keep the per-run append_instructions addendum
# ---------------------------------------------------------------------------


class _DropSystemPolicy:
    """Always returns a changed view with the leading system entry removed."""

    async def compact(self, req: CompactionRequest) -> ContextResult:
        entries = [
            e
            for e in req.entries
            if not (isinstance(e, InputEntry) and e.role == "system")
        ]
        return ContextResult(entries=entries, changed=True, reason="test")


async def test_compacted_view_keeps_append_instructions() -> None:
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="t", instructions="base prompt", model=provider)

    await Runner.run(
        agent,
        "hi",
        append_instructions="SECRET-ADDENDUM",
        context_policy=_DropSystemPolicy(),
    )

    system_msg = provider.calls[0][0]
    assert system_msg.role == "system"
    assert "base prompt" in system_msg.content
    assert "SECRET-ADDENDUM" in system_msg.content


# ---------------------------------------------------------------------------
# Budget-limited runs checkpoint as interrupted and can be resumed
# ---------------------------------------------------------------------------


async def test_budget_exceeded_is_interrupted_and_resumable() -> None:
    provider = ScriptedProvider(
        [
            call("add", {"a": 1, "b": 1}, call_id="c1"),
            call("add", {"a": 2, "b": 2}, call_id="c2"),
            text("done"),
        ]
    )
    agent = Agent(name="t", model=provider, tools=[add])
    cp = InMemoryCheckpointer()

    with pytest.raises(BudgetExceeded):
        await Runner.run(
            agent,
            "go",
            checkpointer=cp,
            run_id="budgeted",
            budget=RunBudget(max_tool_calls=1),
        )

    snap = await cp.load("budgeted")
    assert snap is not None
    assert snap.status == "interrupted"

    # Resume without the tight budget: the pending call drains, then the
    # remaining script completes the run.
    result = await Runner.run(
        agent, [], checkpointer=cp, run_id="budgeted", if_run_exists="require"
    )
    assert result.output == "done"


# ---------------------------------------------------------------------------
# entries_to_messages concatenates consecutive assistant text entries
# ---------------------------------------------------------------------------


def test_entries_to_messages_concatenates_consecutive_text() -> None:
    messages = entries_to_messages(
        [AssistantTextEntry(content="Hello, "), AssistantTextEntry(content="world")]
    )
    assert [m.role for m in messages] == ["assistant"]
    assert messages[0].content == "Hello, world"


# ---------------------------------------------------------------------------
# A context overflow after output reached the consumer is not retried
# ---------------------------------------------------------------------------


class _MidStreamOverflowProvider:
    """Streams a delta, then raises ContextOverflowError."""

    name = "midstream-overflow"
    model = "fake-model"

    def __init__(self) -> None:
        self.stream_count = 0

    async def stream(self, entries, *, tools=None, response_format=None, settings=None):
        self.stream_count += 1
        yield TextDelta(text="partial")
        raise ContextOverflowError("simulated mid-stream overflow")


async def test_overflow_after_forwarded_output_is_not_retried() -> None:
    provider = _MidStreamOverflowProvider()
    agent = Agent(name="t", instructions="x", model=provider)

    with pytest.raises(ContextOverflowError):
        await Runner.run(agent, "go")

    # No silent re-stream: the provider was called exactly once.
    assert provider.stream_count == 1


# ---------------------------------------------------------------------------
# Lenient JSON parsing tolerates markdown fences around structured output
# ---------------------------------------------------------------------------


async def test_structured_output_parses_fenced_json() -> None:
    class Out(BaseModel):
        value: int

    provider = ScriptedProvider([text('```json\n{"value": 42}\n```')])
    agent = Agent(name="t", model=provider, output_type=Out)

    result = await Runner.run(agent, "go")
    assert isinstance(result.output, Out)
    assert result.output.value == 42


# ---------------------------------------------------------------------------
# Resuming a session-backed run persists the transcript on completion
# ---------------------------------------------------------------------------


async def test_resume_persists_session_history() -> None:
    provider = ScriptedProvider(
        [
            call("add", {"a": 1, "b": 1}, call_id="c1"),
            call("add", {"a": 2, "b": 2}, call_id="c2"),
            text("done"),
        ]
    )
    agent = Agent(name="t", model=provider, tools=[add])
    cp = InMemoryCheckpointer()
    session = InMemorySession()

    with pytest.raises(BudgetExceeded):
        await Runner.run(
            agent,
            "go",
            checkpointer=cp,
            run_id="sessioned",
            session=session,
            session_id="s1",
            budget=RunBudget(max_tool_calls=1),
        )

    result = await Runner.run(
        agent,
        [],
        checkpointer=cp,
        run_id="sessioned",
        session=session,
        session_id="s1",
        if_run_exists="require",
    )

    assert result.output == "done"
    history = await session.load("s1")
    assert history, "resumed run must write its transcript back to the session"
    final_messages = entries_to_messages(history)
    assert any(m.role == "assistant" and m.content == "done" for m in final_messages)


# ---------------------------------------------------------------------------
# A cancelled run still persists an interrupted (resumable) snapshot
# ---------------------------------------------------------------------------


class _CancelMidStreamProvider:
    """Yields one delta, then raises CancelledError (simulated cancellation)."""

    name = "cancel-midstream"
    model = "fake-model"

    async def stream(self, entries, *, tools=None, response_format=None, settings=None):
        yield TextDelta(text="partial")
        raise asyncio.CancelledError()


async def test_cancelled_run_persists_interrupted_snapshot() -> None:
    provider = _CancelMidStreamProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    cp = InMemoryCheckpointer()

    with pytest.raises(asyncio.CancelledError):
        await Runner.run(agent, "go", checkpointer=cp, run_id="cancelled")

    snap = await cp.load("cancelled")
    assert snap is not None
    assert snap.status == "interrupted"
