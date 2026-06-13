"""Phase 9b: entries_log mirrors the transcript across all runner paths.

Invariant the runner now maintains:
    entries_to_messages(result.entries) == result.messages

(modulo intentional structural normalization — see below.)

These tests cover every transcript-mutation site (initial input, assistant
turn, tool call, tool error, denied approval, repair-attempt user prompt,
handoff transcript reset, resume from snapshot).
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest

from lovia import (
    Agent,
    Handoff,
    Runner,
    entries_to_messages,
    tool,
)
from lovia.transcript import (
    FinishDelta,
    InputEntry,
    TranscriptEntry,
    ModelDelta,
    AssistantTextEntry,
    ReasoningEntry,
    TextDelta,
    ToolCallEntry,
    ToolResultEntry,
    UsageDelta,
)
from lovia.messages import Message, Usage
from lovia.stores import InMemorySession

from .scripted_provider import ScriptedProvider, call, text


def _normalize(msgs: list[Message]) -> list[tuple]:
    """Compare-by-shape: role, content text, tool_calls (id+name+args), tool_call_id.

    ``content`` may be a list[ContentPart] which would compare structurally;
    flattening keeps the assertion focused on the structure we care about.
    """
    from lovia.content import text_of

    out: list[tuple] = []
    for m in msgs:
        out.append(
            (
                m.role,
                text_of(m.content) if m.content is not None else None,
                tuple((tc.id, tc.name, tc.arguments) for tc in m.tool_calls),
                m.tool_call_id,
            )
        )
    return tuple(out)  # type: ignore[return-value]


@tool
async def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
async def boom() -> str:
    """Always raises."""
    raise RuntimeError("kaboom")


async def test_entries_mirror_simple_text() -> None:
    provider = ScriptedProvider([text("hi back")])
    agent = Agent(name="t", instructions="be brief", model=provider)
    result = await Runner.run(agent, "hello")
    # The TranscriptEntry view round-trips back to the same transcript shape.
    assert _normalize(entries_to_messages(result.entries)) == _normalize(
        result.messages
    )
    # And the structure is what we expect: system, user, assistant.
    kinds = [type(i).__name__ for i in result.entries]
    assert kinds == ["InputEntry", "InputEntry", "AssistantTextEntry"]


async def test_entries_mirror_tool_call_and_reply() -> None:
    provider = ScriptedProvider(
        [call("add", {"a": 1, "b": 2}), text("the answer is 3")]
    )
    agent = Agent(name="t", instructions="use tools", model=provider, tools=[add])
    result = await Runner.run(agent, "1 + 2 = ?")
    assert _normalize(entries_to_messages(result.entries)) == _normalize(
        result.messages
    )
    # Tool output preserves the raw int return.
    tool_outputs = [i for i in result.entries if isinstance(i, ToolResultEntry)]
    assert len(tool_outputs) == 1
    assert tool_outputs[0].raw == 3
    assert tool_outputs[0].output == "3"
    assert tool_outputs[0].is_error is False


async def test_entries_mirror_tool_error() -> None:
    provider = ScriptedProvider([call("boom", {}), text("ok, recovered")])
    agent = Agent(name="t", instructions="x", model=provider, tools=[boom])
    result = await Runner.run(agent, "go")
    assert _normalize(entries_to_messages(result.entries)) == _normalize(
        result.messages
    )
    tool_out = next(i for i in result.entries if isinstance(i, ToolResultEntry))
    assert tool_out.is_error is True
    assert "kaboom" in tool_out.output


async def test_entries_mirror_reasoning() -> None:
    provider = ScriptedProvider([text("here is the answer", reasoning="thinking...")])
    agent = Agent(name="t", instructions="x", model=provider)
    result = await Runner.run(agent, "q")
    # Reasoning becomes its own TranscriptEntry, appearing before the message output.
    kinds = [type(i).__name__ for i in result.entries]
    assert kinds == [
        "InputEntry",
        "InputEntry",
        "ReasoningEntry",
        "AssistantTextEntry",
    ]
    # The forward direction (entries -> messages) re-merges reasoning + content
    # into a single assistant Message, matching ``result.messages``.
    assert _normalize(entries_to_messages(result.entries)) == _normalize(
        result.messages
    )


async def test_entries_mirror_handoff_transcript_reset() -> None:
    specialist_provider = ScriptedProvider([text("specialist here")])
    specialist = Agent(
        name="Specialist", instructions="be specialist", model=specialist_provider
    )

    triage_provider = ScriptedProvider(
        [call("transfer_to_specialist", {})]  # handoff tool
    )
    triage = Agent(
        name="Triage",
        instructions="route",
        model=triage_provider,
        handoffs=[Handoff(specialist)],
    )

    result = await Runner.run(triage, "help me")
    # After a handoff, ``transcript`` is rewritten by ``_reset_for_handoff``;
    # the entries_log mirror must follow.
    assert _normalize(entries_to_messages(result.entries)) == _normalize(
        result.messages
    )
    assert result.final_agent.name == "Specialist"


async def test_entries_mirror_repair_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the model emits invalid structured output, a user repair prompt
    is appended to the transcript — entries_log must mirror that."""
    from pydantic import BaseModel

    class Out(BaseModel):
        value: int

    # First reply has invalid JSON, second is valid.
    provider = ScriptedProvider([text("not json"), text(json.dumps({"value": 7}))])
    agent = Agent(name="t", instructions="x", model=provider, output_type=Out)
    result = await Runner.run(agent, "give me a number")
    assert isinstance(result.output, Out) and result.output.value == 7
    # Repair prompt appears as a user entry between the two assistant entries.
    user_entries = [
        i for i in result.entries if isinstance(i, InputEntry) and i.role == "user"
    ]
    # original user + repair prompt = 2 user entries
    assert len(user_entries) == 2
    assert _normalize(entries_to_messages(result.entries)) == _normalize(
        result.messages
    )


async def test_entries_mirror_resume_from_snapshot() -> None:
    """Resume rebuilds the transcript from the snapshot's entries; the
    mirror must still round-trip."""
    from lovia import InputEntry, AssistantTextEntry, InMemoryCheckpointer
    from lovia.checkpointer import RunSnapshot
    from lovia.messages import Usage

    # Build a snapshot mid-conversation: system + user + assistant + user.
    snap_entries = [
        InputEntry(role="system", content="be helpful"),
        InputEntry(role="user", content="2+2?"),
        AssistantTextEntry(content="4"),
        InputEntry(role="user", content="thanks"),
    ]

    snap = RunSnapshot(
        run_id="r1", agent_name="t", entries=snap_entries, usage=Usage(), turns=1
    )
    cp = InMemoryCheckpointer()
    await cp.save(snap)

    provider = ScriptedProvider([text("you're welcome")])
    agent = Agent(name="t", instructions="be helpful", model=provider)
    result = await Runner.run(
        agent, [], checkpointer=cp, run_id="r1", if_run_exists="require"
    )
    assert _normalize(entries_to_messages(result.entries)) == _normalize(
        result.messages
    )
    # Snapshot contributed 4 entries; the assistant turn adds 1.
    assert len(result.entries) == 5


async def test_session_history_preserves_reasoning_entries_for_provider_replay() -> (
    None
):
    class RecordingProvider:
        name = "recording"
        model = "recording-model"
        supports_json_schema = False

        def __init__(self) -> None:
            self.calls: list[list[TranscriptEntry]] = []

        async def stream(
            self,
            entries: list[TranscriptEntry],
            **_: Any,
        ) -> AsyncIterator[ModelDelta]:
            self.calls.append(list(entries))
            yield TextDelta(text="ok")
            yield UsageDelta(usage=Usage(input_tokens=1, output_tokens=1))
            yield FinishDelta(reason="stop")

    session = InMemorySession()
    reasoning = ReasoningEntry(
        id="rs_1",
        content="summary",
        provider="reasoning-provider",
        metadata={"encrypted_content": "enc"},
    )
    await session.append(
        "chat",
        [
            InputEntry(role="user", content="old question"),
            reasoning,
            AssistantTextEntry(content="old answer"),
        ],
    )
    provider = RecordingProvider()
    agent = Agent(name="t", instructions="be helpful", model=provider)

    result = await Runner.run(agent, "new question", session=session, session_id="chat")

    assert result.output == "ok"
    assert reasoning in provider.calls[0]


async def test_assistant_text_entry_preserves_content() -> None:
    provider = ScriptedProvider([text("the answer")])
    agent = Agent(name="t", instructions="x", model=provider)
    result = await Runner.run(agent, "?")
    text_entries = [i for i in result.entries if isinstance(i, AssistantTextEntry)]
    assert text_entries == [AssistantTextEntry(content="the answer")]
    # And no spurious reasoning entry.
    assert not any(isinstance(i, ReasoningEntry) for i in result.entries)


async def test_tool_call_entry_carries_call_metadata() -> None:
    provider = ScriptedProvider(
        [call("add", {"a": 5, "b": 7}, call_id="my_id"), text("12")]
    )
    agent = Agent(name="t", instructions="x", model=provider, tools=[add])
    result = await Runner.run(agent, "add")
    call_entries = [i for i in result.entries if isinstance(i, ToolCallEntry)]
    assert len(call_entries) == 1
    assert call_entries[0].call_id == "my_id"
    assert call_entries[0].name == "add"
    assert json.loads(call_entries[0].arguments) == {"a": 5, "b": 7}
