"""Regression tests for the lovia.runtime rewrite."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel

from lovia import Agent, CheckpointOptions, Handoff, Runner, tool
from lovia.exceptions import BudgetExceeded, ContextOverflowError, UserError
from lovia.plugins.mcp import MCP
from lovia.messages import AssistantTurn, ToolCall, Usage, system, user
from lovia.reliability import RunBudget
from lovia.stores import InMemoryCheckpointer, InMemorySession
from lovia.context import CompactionRequest, ContextResult
from lovia.transcript import (
    AssistantTextEntry,
    FinishDelta,
    InputEntry,
    TextDelta,
    ToolCallDelta,
    entries_to_messages,
)

from ..scripted_provider import ScriptedProvider, call, text


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
        plugins=[MCP(server)],
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
# Handoff + caller-supplied system input: both systems reach the new agent
# (the adapter merges them into one ``system`` param — no provider rejection)
# ---------------------------------------------------------------------------


async def test_handoff_surfaces_both_caller_and_target_system_to_model() -> None:
    # A systemless agent carrying a caller-supplied leading system() input hands
    # off to an agent that DOES render a system prompt. The handoff preserves the
    # caller's system (it is run content, not the runner's head), so the new
    # agent's view leads with TWO system entries: the target's head, then the
    # caller's. That is exactly the shape ``_to_anthropic_messages`` collapses
    # into a single ``system`` param (see
    # tests/providers/test_anthropic.py::test_message_translation_extracts_system_and_tool_blocks),
    # and OpenAI merges adjacent same-role messages — so no provider rejects it.
    specialist = Agent(
        name="specialist",
        instructions="B-SYS",
        model=ScriptedProvider([text("final")]),
    )
    triage = Agent(
        name="triage",  # systemless: no runner head of its own
        model=ScriptedProvider(
            [call("transfer_to_specialist", {"reason": "x"}, call_id="c1")]
        ),
        handoffs=[Handoff(target=specialist)],
    )

    result = await Runner.run(triage, [system("USER-SYS"), user("hi")])
    assert result.output == "final"

    # The specialist's first model call sees both systems — target head first,
    # the caller's preserved next — then the user turn.
    inbox = specialist.model.calls[0]  # type: ignore[attr-defined]
    assert [m.content for m in inbox if m.role == "system"] == ["B-SYS", "USER-SYS"]
    assert [m.role for m in inbox[:3]] == ["system", "system", "user"]


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


async def test_malformed_tool_call_is_normalized_before_being_re_sent() -> None:
    # A stream truncated mid-call leaves invalid-JSON arguments. They must be
    # normalized in the transcript so the *next* request — which re-sends the
    # whole history — is valid JSON, not a provider-side 400 waiting to happen.
    bad = AssistantTurn(
        content=None,
        tool_calls=[ToolCall(id="c1", name="add", arguments='{"a": 1')],
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    provider = ScriptedProvider([bad, text("ok")])
    agent = Agent(name="t", model=provider, tools=[add])

    await Runner.run(agent, "go")

    # calls[1] = the second model call; its history carries the tool call.
    resent = provider.calls[1]
    assistant = next(m for m in resent if m.role == "assistant" and m.tool_calls)
    args = assistant.tool_calls[0].arguments
    assert json.loads(args) == {"_raw": '{"a": 1'}  # valid JSON, original kept


async def test_valid_but_non_object_tool_args_are_normalized_before_re_send() -> None:
    # Valid JSON that isn't an object (here a bare array) parses cleanly, so the
    # bad-JSON gate never fires — but Anthropic unpacks arguments into
    # tool_use.input, which must be an object, and 400s on a bare array/scalar.
    # Normalization must wrap these too, not only unparseable text. Rejected as
    # an unknown tool so the (unmodified) call reaches re-serialization.
    bad = AssistantTurn(
        content=None,
        tool_calls=[ToolCall(id="c1", name="ghost", arguments="[1, 2]")],
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    provider = ScriptedProvider([bad, text("ok")])
    agent = Agent(name="t", model=provider, tools=[add])

    await Runner.run(agent, "go")

    resent = provider.calls[1]
    assistant = next(m for m in resent if m.role == "assistant" and m.tool_calls)
    args = assistant.tool_calls[0].arguments
    assert json.loads(args) == {"_raw": "[1, 2]"}  # wrapped into a JSON object


async def test_length_truncated_tool_call_tells_model_it_hit_the_token_limit() -> None:
    # finish_reason="length" cut the call off mid-arguments. The model must
    # learn it was truncated (and to chunk) — not that its JSON was merely
    # "invalid", which would send it looping on the same oversized call.
    bad = AssistantTurn(
        content=None,
        tool_calls=[ToolCall(id="c1", name="add", arguments='{"a": 1')],
        usage=Usage(input_tokens=1, output_tokens=1),
        finish_reason="length",
    )
    provider = ScriptedProvider([bad, text("ok")])
    agent = Agent(name="t", model=provider, tools=[add])

    result = await Runner.run(agent, "go")

    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "output token limit" in tool_msg.content
    assert "chunk" in tool_msg.content
    assert "Invalid JSON" not in tool_msg.content


# ---------------------------------------------------------------------------
# Compacted views keep the per-run extra_instructions addendum
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


async def test_compacted_view_keeps_extra_instructions() -> None:
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="t", instructions="base prompt", model=provider)

    await Runner.run(
        agent,
        "hi",
        extra_instructions="SECRET-ADDENDUM",
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
            checkpoint=CheckpointOptions(cp, "budgeted"),
            budget=RunBudget(max_tool_calls=1),
        )

    snap = await cp.load("budgeted")
    assert snap is not None
    assert snap.status == "interrupted"

    # Resume without the tight budget: the pending call drains, then the
    # remaining script completes the run.
    result = await Runner.run(
        agent,
        [],
        checkpoint=CheckpointOptions(cp, "budgeted", if_run_exists="resume_only"),
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
# A pre-output overflow is retried reactively, and the runner forwards the
# endpoint's reported window into the policy — the only learning path since
# providers no longer memoize enforced windows themselves.
# ---------------------------------------------------------------------------


class _RecoveringOverflowProvider:
    """Call 1: request a huge tool result. Call 2: reject the prompt naming
    the window. Call 3 (the reactive retry): answer."""

    name = "overflow-recovers"
    model = "fake-model"

    def __init__(self) -> None:
        self.stream_count = 0

    async def stream(self, entries, *, tools=None, response_format=None, settings=None):
        self.stream_count += 1
        if self.stream_count == 1:
            yield ToolCallDelta(index=0, call_id="c1", name="dump", arguments="{}")
            yield FinishDelta(reason="tool_calls")
        elif self.stream_count == 2:
            err = ContextOverflowError(
                "prompt is too long: 12000 tokens > 8192 maximum"
            )
            err.reported_window = 8_192
            raise err
        else:
            yield TextDelta(text="recovered")
            yield FinishDelta(reason="stop")


async def test_reactive_overflow_forwards_the_reported_window(caplog) -> None:
    import logging

    @tool
    async def dump() -> str:
        """Return a huge payload."""
        return "x" * 40_000

    provider = _RecoveringOverflowProvider()
    agent = Agent(name="t", instructions="x", model=provider, tools=[dump])

    with caplog.at_level(logging.INFO, logger="lovia.context.compaction"):
        result = await Runner.run(agent, "go")

    # The reactive compact shrank the view (the oversized tool result gave it
    # something to cut) and the retry succeeded.
    assert result.output == "recovered"
    assert provider.stream_count == 3
    # The window the endpoint named in its rejection reached the policy via
    # request.reported_window — pipeline tests inject the field directly, so
    # this is the one place the runner-side forwarding is exercised.
    assert any("learned 8192 tokens" in rec.getMessage() for rec in caplog.records)


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
            checkpoint=CheckpointOptions(cp, "sessioned"),
            session=session,
            session_id="s1",
            budget=RunBudget(max_tool_calls=1),
        )

    result = await Runner.run(
        agent,
        [],
        checkpoint=CheckpointOptions(cp, "sessioned", if_run_exists="resume_only"),
        session=session,
        session_id="s1",
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
        await Runner.run(agent, "go", checkpoint=CheckpointOptions(cp, "cancelled"))

    snap = await cp.load("cancelled")
    assert snap is not None
    assert snap.status == "interrupted"


# ---------------------------------------------------------------------------
# A run that handed off resumes as the *target* agent (handoff-graph resolution)
# ---------------------------------------------------------------------------


@tool
async def specialist_only(x: int) -> int:
    """A tool only the specialist agent registers."""
    return x * 10


def _triage_to_specialist() -> tuple[Agent, Agent]:
    """Triage that immediately transfers to a specialist with its own tool."""
    specialist = Agent(
        name="Specialist",
        instructions="SPECIALIST-BASE",
        model=ScriptedProvider(
            [
                call("specialist_only", {"x": 2}, call_id="s1"),
                text("done by specialist"),
            ]
        ),
        tools=[specialist_only],
    )
    triage = Agent(
        name="Triage",
        instructions="TRIAGE-BASE",
        model=ScriptedProvider(
            [call("transfer_to_specialist", {"reason": "needs help"}, call_id="t1")]
        ),
        handoffs=[specialist],
    )
    return triage, specialist


async def test_resume_after_handoff_continues_as_target_agent() -> None:
    triage, specialist = _triage_to_specialist()
    cp = InMemoryCheckpointer()

    # max_tool_calls=1: the transfer consumes the budget, so the specialist's
    # own tool call trips it — interrupting *after* the handoff, with a snapshot
    # whose active agent is the specialist.
    with pytest.raises(BudgetExceeded):
        await Runner.run(
            triage,
            "go",
            checkpoint=CheckpointOptions(cp, "h1"),
            budget=RunBudget(max_tool_calls=1),
        )

    snap = await cp.load("h1")
    assert snap is not None
    assert snap.status == "interrupted"
    # The snapshot records the *target* agent — the case that used to make
    # resume impossible.
    assert snap.agent_name == "Specialist"

    # Resume with the entry agent: the loop resolves the active agent from the
    # handoff graph, drains the pending specialist tool call against the
    # specialist's tools, and completes as the specialist.
    result = await Runner.run(
        triage,
        [],
        checkpoint=CheckpointOptions(cp, "h1", if_run_exists="resume_only"),
    )
    assert result.output == "done by specialist"
    assert result.final_agent.name == "Specialist"


async def test_resume_with_unreachable_entry_agent_raises() -> None:
    triage, _ = _triage_to_specialist()
    cp = InMemoryCheckpointer()
    with pytest.raises(BudgetExceeded):
        await Runner.run(
            triage,
            "go",
            checkpoint=CheckpointOptions(cp, "h2"),
            budget=RunBudget(max_tool_calls=1),
        )

    # An unrelated entry agent cannot reach "Specialist" through its handoffs,
    # so resume fails fast with a clear error instead of silently mis-running.
    other = Agent(name="Other", model=ScriptedProvider([text("noop")]))
    with pytest.raises(UserError, match="not reachable"):
        await Runner.run(
            other,
            [],
            checkpoint=CheckpointOptions(cp, "h2", if_run_exists="resume_only"),
        )


# ---------------------------------------------------------------------------
# extra_instructions is run-scoped and carries across a handoff
# ---------------------------------------------------------------------------


async def test_extra_instructions_persist_across_handoff() -> None:
    specialist = Agent(
        name="Specialist",
        instructions="SPECIALIST-BASE",
        model=ScriptedProvider([text("ok")]),
    )
    triage = Agent(
        name="Triage",
        instructions="TRIAGE-BASE",
        model=ScriptedProvider(
            [call("transfer_to_specialist", {"reason": "x"}, call_id="t1")]
        ),
        handoffs=[specialist],
    )

    await Runner.run(triage, "go", extra_instructions="RUN-ADDENDUM")

    # The specialist's first model call must see a system prompt carrying both
    # its own instructions and the run-level addendum (not just the triage's).
    system_msg = specialist.model.calls[0][0]  # type: ignore[attr-defined]
    assert system_msg.role == "system"
    assert "SPECIALIST-BASE" in system_msg.content
    assert "RUN-ADDENDUM" in system_msg.content


# ---------------------------------------------------------------------------
# A completed multi-hop handoff run replays as the deepest agent
# ---------------------------------------------------------------------------


async def test_completed_multi_hop_handoff_replays_as_deepest_agent() -> None:
    third = Agent(name="Third", model=ScriptedProvider([text("done by third")]))
    second = Agent(
        name="Second",
        model=ScriptedProvider(
            [call("transfer_to_third", {"reason": "x"}, call_id="h2")]
        ),
        handoffs=[third],
    )
    first = Agent(
        name="First",
        model=ScriptedProvider(
            [call("transfer_to_second", {"reason": "x"}, call_id="h1")]
        ),
        handoffs=[second],
    )
    cp = InMemoryCheckpointer()

    result = await Runner.run(first, "go", checkpoint=CheckpointOptions(cp, "multi"))
    assert result.output == "done by third"
    assert result.final_agent.name == "Third"

    snap = await cp.load("multi")
    assert snap is not None
    assert snap.status == "completed"
    assert snap.agent_name == "Third"

    # Replaying the completed run resolves the deepest agent (First→Second→Third)
    # from the entry agent's handoff graph for final_agent and output coercion —
    # without touching the (already-exhausted) providers.
    replay = await Runner.run(
        first,
        "ignored",
        checkpoint=CheckpointOptions(cp, "multi", if_run_exists="resume"),
    )
    assert replay.output == "done by third"
    assert replay.final_agent.name == "Third"
