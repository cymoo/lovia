"""Tests for mid-run message injection — the :class:`Mailbox` steering channel."""

from __future__ import annotations

from lovia import Agent, Mailbox, Runner, events, tool
from lovia.messages import Usage
from lovia.stores import InMemorySession
from lovia.transcript import FinishDelta, TextDelta, UsageDelta

from .scripted_provider import ScriptedProvider, call, text


# ----------------------------------------------------------- primitive ----


def test_mailbox_push_drain_is_fifo() -> None:
    m = Mailbox()
    assert not m
    m.push("a")
    m.push("b")
    assert bool(m)
    assert m.drain() == ["a", "b"]
    assert m.drain() == []  # draining is destructive
    assert not m


def test_mailbox_remove_withdraws_a_queued_item() -> None:
    m = Mailbox()
    t1 = m.push("a")
    m.push("b")
    assert m.remove(t1) is True
    assert m.remove(t1) is False  # already withdrawn
    assert m.remove(999) is False  # unknown token
    assert m.drain() == ["b"]  # only the un-withdrawn item remains


# ------------------------------------------------------------- loop -------


async def _collect(handle):
    """Drive a stream to completion; return (events, RunResult)."""
    seen: list[events.Event] = []
    async for ev in handle:
        seen.append(ev)
    result = next(ev.result for ev in seen if isinstance(ev, events.RunCompleted))
    return seen, result


async def test_injected_message_consumed_at_next_turn_start() -> None:
    mailbox = Mailbox()

    @tool
    async def trip() -> str:
        """Queue a follow-up, then return."""
        mailbox.push("and another thing")
        return "ok"

    provider = ScriptedProvider([call("trip", {}, call_id="c1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[trip])

    seen, result = await _collect(Runner.stream(agent, "go", mailbox=mailbox))

    assert result.output == "done"
    assert result.turns == 2
    # The injected message lands as a user turn between the tool result and the
    # final answer (no system message — this agent has no instructions).
    assert [m.role for m in result.messages] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    assert result.messages[3].content == "and another thing"
    # Exactly one injection event, tagged with the turn that consumed it.
    injected = [ev for ev in seen if isinstance(ev, events.UserMessageInjected)]
    assert len(injected) == 1
    assert injected[0].turn == 2
    assert injected[0].content == "and another thing"
    # The model actually saw it on its turn-2 call.
    turn2_users = [m.content for m in provider.calls[1] if m.role == "user"]
    assert "and another thing" in turn2_users


async def test_multiple_injected_messages_preserve_fifo_order() -> None:
    mailbox = Mailbox()

    @tool
    async def trip() -> str:
        """Queue two follow-ups."""
        mailbox.push("first")
        mailbox.push("second")
        return "ok"

    provider = ScriptedProvider([call("trip", {}, call_id="c1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[trip])

    seen, result = await _collect(Runner.stream(agent, "go", mailbox=mailbox))

    injected = [ev.content for ev in seen if isinstance(ev, events.UserMessageInjected)]
    assert injected == ["first", "second"]
    user_contents = [m.content for m in result.messages if m.role == "user"]
    assert user_contents == ["go", "first", "second"]  # original then both, in order


async def test_message_pushed_after_last_drain_stays_queued() -> None:
    # A provider that pushes into the mailbox *during* its (only) model call —
    # i.e. after the turn-start drain — and returns a final answer with no tool
    # call, so the run ends with no next turn to consume the message.
    mailbox = Mailbox()

    class LatePushProvider:
        name = "late"
        supports_json_schema = False

        async def stream(
            self, entries, *, tools=None, response_format=None, settings=None
        ):
            mailbox.push("too late")
            yield TextDelta(text="final")
            yield UsageDelta(usage=Usage(input_tokens=1, output_tokens=1))
            yield FinishDelta(reason="stop")

    agent = Agent(name="t", model=LatePushProvider())
    seen, result = await _collect(Runner.stream(agent, "go", mailbox=mailbox))

    assert result.output == "final"
    assert result.turns == 1
    # Nothing was injected this run...
    assert not any(isinstance(ev, events.UserMessageInjected) for ev in seen)
    # ...the late message is still queued for whoever runs next.
    assert mailbox.drain() == ["too late"]


async def test_no_mailbox_is_inert() -> None:
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider)
    seen, result = await _collect(Runner.stream(agent, "go"))
    assert result.output == "hi"
    assert not any(isinstance(ev, events.UserMessageInjected) for ev in seen)


async def test_injected_message_persists_to_session_once() -> None:
    mailbox = Mailbox()
    session = InMemorySession()

    @tool
    async def trip() -> str:
        """Queue a follow-up."""
        mailbox.push("remember this")
        return "ok"

    provider = ScriptedProvider([call("trip", {}, call_id="c1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[trip])
    await Runner.run(agent, "go", mailbox=mailbox, session=session, session_id="s1")

    # Reloading the session for a follow-up run shows the injected message once.
    second = ScriptedProvider([text("ok2")])
    await Runner.run(
        Agent(name="t", model=second), "next", session=session, session_id="s1"
    )
    loaded_users = [m.content for m in second.calls[0] if m.role == "user"]
    assert loaded_users.count("remember this") == 1
