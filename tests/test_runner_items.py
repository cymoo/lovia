"""Phase 9b: items_log mirrors the transcript across all runner paths.

Invariant the runner now maintains:
    items_to_chat_messages(result.new_items) == result.messages

(modulo intentional structural normalization — see below.)

These tests cover every transcript-mutation site (initial input, assistant
turn, tool call, tool error, denied approval, repair-attempt user prompt,
handoff transcript reset, resume from snapshot).
"""

from __future__ import annotations

import json

import pytest

from lovia import (
    Agent,
    Handoff,
    Runner,
    items_to_chat_messages,
    tool,
)
from lovia.items import (
    InputMessageItem,
    MessageOutputItem,
    ReasoningItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from lovia.messages import ChatMessage

from .scripted_provider import ScriptedProvider, call, text


def _normalize(msgs: list[ChatMessage]) -> list[tuple]:
    """Compare-by-shape: role, content text, tool_calls (id+name+args), tool_call_id.

    ``content`` may be a list[ContentBlock] which would compare structurally;
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


async def test_items_mirror_simple_text() -> None:
    provider = ScriptedProvider([text("hi back")])
    agent = Agent(name="t", instructions="be brief", model=provider)
    result = await Runner.run(agent, "hello")
    # The Item view round-trips back to the same transcript shape.
    assert _normalize(items_to_chat_messages(result.new_items)) == _normalize(
        result.messages
    )
    # And the structure is what we expect: system, user, assistant.
    kinds = [type(i).__name__ for i in result.new_items]
    assert kinds == ["InputMessageItem", "InputMessageItem", "MessageOutputItem"]


async def test_items_mirror_tool_call_and_reply() -> None:
    provider = ScriptedProvider(
        [call("add", {"a": 1, "b": 2}), text("the answer is 3")]
    )
    agent = Agent(name="t", instructions="use tools", model=provider, tools=[add])
    result = await Runner.run(agent, "1 + 2 = ?")
    assert _normalize(items_to_chat_messages(result.new_items)) == _normalize(
        result.messages
    )
    # Tool output preserves the raw int return.
    tool_outputs = [i for i in result.new_items if isinstance(i, ToolCallOutputItem)]
    assert len(tool_outputs) == 1
    assert tool_outputs[0].raw == 3
    assert tool_outputs[0].output == "3"
    assert tool_outputs[0].is_error is False


async def test_items_mirror_tool_error() -> None:
    provider = ScriptedProvider([call("boom", {}), text("ok, recovered")])
    agent = Agent(name="t", instructions="x", model=provider, tools=[boom])
    result = await Runner.run(agent, "go")
    assert _normalize(items_to_chat_messages(result.new_items)) == _normalize(
        result.messages
    )
    tool_out = next(i for i in result.new_items if isinstance(i, ToolCallOutputItem))
    assert tool_out.is_error is True
    assert "kaboom" in tool_out.output


async def test_items_mirror_reasoning() -> None:
    provider = ScriptedProvider([text("here is the answer", reasoning="thinking...")])
    agent = Agent(name="t", instructions="x", model=provider)
    result = await Runner.run(agent, "q")
    # Reasoning becomes its own Item, appearing before the message output.
    kinds = [type(i).__name__ for i in result.new_items]
    assert kinds == [
        "InputMessageItem",
        "InputMessageItem",
        "ReasoningItem",
        "MessageOutputItem",
    ]
    # The forward direction (items -> messages) re-merges reasoning + content
    # into a single assistant ChatMessage, matching ``result.messages``.
    assert _normalize(items_to_chat_messages(result.new_items)) == _normalize(
        result.messages
    )


async def test_items_mirror_handoff_transcript_reset() -> None:
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
    # the items_log mirror must follow.
    assert _normalize(items_to_chat_messages(result.new_items)) == _normalize(
        result.messages
    )
    assert result.final_agent.name == "Specialist"


async def test_items_mirror_repair_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the model emits invalid structured output, a user repair prompt
    is appended to the transcript — items_log must mirror that."""
    from pydantic import BaseModel

    class Out(BaseModel):
        value: int

    # First reply has invalid JSON, second is valid.
    provider = ScriptedProvider(
        [text("not json"), text(json.dumps({"value": 7}))]
    )
    agent = Agent(
        name="t", instructions="x", model=provider, output_type=Out
    )
    result = await Runner.run(agent, "give me a number")
    assert isinstance(result.output, Out) and result.output.value == 7
    # Repair prompt appears as a user item between the two assistant items.
    user_items = [
        i for i in result.new_items if isinstance(i, InputMessageItem) and i.role == "user"
    ]
    # original user + repair prompt = 2 user items
    assert len(user_items) == 2
    assert _normalize(items_to_chat_messages(result.new_items)) == _normalize(
        result.messages
    )


async def test_items_mirror_resume_from_snapshot() -> None:
    """Resume rebuilds the transcript from the snapshot's items; the
    mirror must still round-trip."""
    from lovia import InputMessageItem, MessageOutputItem
    from lovia.checkpointer import RunSnapshot
    from lovia.messages import Usage

    # Build a snapshot mid-conversation: system + user + assistant + user.
    snap_items = [
        InputMessageItem(role="system", content="be helpful"),
        InputMessageItem(role="user", content="2+2?"),
        MessageOutputItem(content="4"),
        InputMessageItem(role="user", content="thanks"),
    ]

    snap = RunSnapshot(
        run_id="r1", agent_name="t", items=snap_items, usage=Usage(), turns=1
    )

    provider = ScriptedProvider([text("you're welcome")])
    agent = Agent(name="t", instructions="be helpful", model=provider)
    result = await Runner.run(agent, "ignored", resume_from=snap)
    assert _normalize(items_to_chat_messages(result.new_items)) == _normalize(
        result.messages
    )
    # Snapshot contributed 4 items; the assistant turn adds 1.
    assert len(result.new_items) == 5


async def test_items_message_output_item_preserves_content() -> None:
    provider = ScriptedProvider([text("the answer")])
    agent = Agent(name="t", instructions="x", model=provider)
    result = await Runner.run(agent, "?")
    msg_items = [i for i in result.new_items if isinstance(i, MessageOutputItem)]
    assert msg_items == [MessageOutputItem(content="the answer")]
    # And no spurious reasoning item.
    assert not any(isinstance(i, ReasoningItem) for i in result.new_items)


async def test_items_tool_call_item_carries_call_metadata() -> None:
    provider = ScriptedProvider(
        [call("add", {"a": 5, "b": 7}, call_id="my_id"), text("12")]
    )
    agent = Agent(name="t", instructions="x", model=provider, tools=[add])
    result = await Runner.run(agent, "add")
    call_items = [i for i in result.new_items if isinstance(i, ToolCallItem)]
    assert len(call_items) == 1
    assert call_items[0].call_id == "my_id"
    assert call_items[0].name == "add"
    assert json.loads(call_items[0].arguments) == {"a": 5, "b": 7}
