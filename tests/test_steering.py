"""Tests for mid-run message injection — the :class:`Mailbox` steering channel."""

from __future__ import annotations

from lovia import Agent, AgentHooks, Mailbox, RunContext, Runner, events, tool
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


async def test_run_without_pushes_emits_no_injection_events() -> None:
    # No mailbox= passed: the runner still creates one (exposed as
    # ``ctx.mailbox``), but with nothing pushed the feature stays invisible.
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider)
    seen, result = await _collect(Runner.stream(agent, "go"))
    assert result.output == "hi"
    assert not any(isinstance(ev, events.UserMessageInjected) for ev in seen)


# ------------------------------------------------ ctx.mailbox steering ----


async def test_tool_steers_via_ctx_mailbox_without_caller_mailbox() -> None:
    # No mailbox= passed: the tool reaches the runner-created default through
    # its RunContext and the push is consumed at the next turn start.
    @tool
    async def trip(ctx: RunContext) -> str:
        """Queue a follow-up via the run's own mailbox."""
        ctx.mailbox.push("and another thing")
        return "ok"

    provider = ScriptedProvider([call("trip", {}, call_id="c1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[trip])

    seen, result = await _collect(Runner.stream(agent, "go"))

    assert result.output == "done"
    injected = [ev for ev in seen if isinstance(ev, events.UserMessageInjected)]
    assert len(injected) == 1
    assert injected[0].turn == 2
    # The model actually saw it on its turn-2 call.
    turn2_users = [m.content for m in provider.calls[1] if m.role == "user"]
    assert "and another thing" in turn2_users


async def test_hook_steers_via_ctx_mailbox() -> None:
    hooks = AgentHooks()

    @hooks.on(events.ToolCallCompleted)
    def nudge(ev: events.ToolCallCompleted, ctx: RunContext) -> None:
        ctx.mailbox.push("also address the deadline")

    @tool
    async def work() -> str:
        """Do the work."""
        return "ok"

    provider = ScriptedProvider([call("work", {}, call_id="c1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[work], hooks=hooks)

    seen, result = await _collect(Runner.stream(agent, "go"))

    injected = [ev for ev in seen if isinstance(ev, events.UserMessageInjected)]
    assert [ev.content for ev in injected] == ["also address the deadline"]
    turn2_users = [m.content for m in provider.calls[1] if m.role == "user"]
    assert "also address the deadline" in turn2_users


async def test_ctx_mailbox_is_the_caller_supplied_instance() -> None:
    # An *empty* Mailbox is falsy (``__bool__``); the runner must still use it
    # rather than swap in a default — pushes via ctx land in the caller's box.
    mailbox = Mailbox()
    seen_boxes: list[Mailbox] = []

    @tool
    async def grab(ctx: RunContext) -> str:
        """Record the run's mailbox."""
        seen_boxes.append(ctx.mailbox)
        return "ok"

    provider = ScriptedProvider([call("grab", {}, call_id="c1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[grab])
    await Runner.run(agent, "go", mailbox=mailbox)

    assert seen_boxes == [mailbox]
    assert seen_boxes[0] is mailbox


async def test_sub_run_does_not_inherit_parent_mailbox() -> None:
    # Deliberate asymmetry with cancel_token (see agent_as_tool): drain() is
    # destructive, so a shared mailbox would let the child's turn boundary
    # steal messages addressed to the parent conversation.
    parent_mailbox = Mailbox()
    child_boxes: list[Mailbox] = []

    @tool
    async def probe(ctx: RunContext) -> str:
        """Record the child run's mailbox; push a message for the parent."""
        child_boxes.append(ctx.mailbox)
        parent_mailbox.push("for the parent")
        return "child tool done"

    child = Agent(
        name="child",
        model=ScriptedProvider([call("probe", {}, call_id="k1"), text("child answer")]),
        tools=[probe],
    )
    parent_provider = ScriptedProvider(
        [call("ask_child", {"input": "go"}, call_id="c1"), text("parent done")]
    )
    parent = Agent(name="parent", model=parent_provider, tools=[child.as_tool()])

    seen, result = await _collect(
        Runner.stream(parent, "delegate", mailbox=parent_mailbox)
    )

    assert result.output == "parent done"
    # The child ran on its own runner-created mailbox, not the parent's.
    assert child_boxes and child_boxes[0] is not parent_mailbox
    # The push was consumed by the *parent's* next turn...
    injected = [ev for ev in seen if isinstance(ev, events.UserMessageInjected)]
    assert [ev.content for ev in injected] == ["for the parent"]
    parent_turn2_users = [
        m.content for m in parent_provider.calls[1] if m.role == "user"
    ]
    assert "for the parent" in parent_turn2_users
    # ...and never leaked into the child's turn-2 view.
    child_provider = child.model
    child_turn2_users = [m.content for m in child_provider.calls[1] if m.role == "user"]
    assert "for the parent" not in child_turn2_users


async def test_default_mailbox_push_on_final_turn_is_dropped() -> None:
    # MessageCompleted on the only turn fires after the last drain; with a
    # runner-created mailbox nobody outside can recover the push. The run must
    # still complete cleanly — the message is simply never seen.
    hooks = AgentHooks()

    @hooks.on(events.MessageCompleted)
    def too_late(ev: events.MessageCompleted, ctx: RunContext) -> None:
        ctx.mailbox.push("nobody will read this")

    provider = ScriptedProvider([text("final")])
    agent = Agent(name="t", model=provider, hooks=hooks)

    seen, result = await _collect(Runner.stream(agent, "go"))

    assert result.output == "final"
    assert result.turns == 1
    assert not any(isinstance(ev, events.UserMessageInjected) for ev in seen)


async def test_hook_push_can_be_withdrawn_before_drain() -> None:
    hooks = AgentHooks()

    @hooks.on(events.ToolCallCompleted)
    def push_and_reconsider(ev: events.ToolCallCompleted, ctx: RunContext) -> None:
        token = ctx.mailbox.push("draft")
        ctx.mailbox.push("keep")
        ctx.mailbox.remove(token)

    @tool
    async def work() -> str:
        """Do the work."""
        return "ok"

    provider = ScriptedProvider([call("work", {}, call_id="c1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[work], hooks=hooks)

    seen, _ = await _collect(Runner.stream(agent, "go"))

    injected = [ev.content for ev in seen if isinstance(ev, events.UserMessageInjected)]
    assert injected == ["keep"]


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
