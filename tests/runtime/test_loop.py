"""Entry-point / bootstrap behaviours of ``lovia.runtime.loop.RunLoop``.

The bulk of the loop is exercised by ``tests/runtime/test_runtime.py`` and the
end-to-end runner tests; this file pins the input-handling edges those don't:
session-id validation and seeding the transcript from a ``list[Message]``.
"""

from __future__ import annotations

import logging

import pytest

from lovia import Agent, Runner, tool
from lovia.exceptions import MaxTurnsExceeded, UserError
from lovia.messages import Message
from lovia.stores import InMemorySession

from ..scripted_provider import ScriptedProvider, call, text


async def test_session_without_session_id_is_rejected() -> None:
    agent = Agent(name="a", model=ScriptedProvider([text("hi")]))
    with pytest.raises(UserError, match="session_id is required"):
        await Runner.run(agent, "go", session=InMemorySession())


async def test_list_of_messages_seeds_the_transcript() -> None:
    provider = ScriptedProvider([text("answer")])
    agent = Agent(name="a", model=provider)
    result = await Runner.run(
        agent,
        [
            Message(role="system", content="be terse"),
            Message(role="user", content="hello there"),
        ],
    )
    assert result.output == "answer"
    # The seeded user message reached the model.
    assert any(
        m.role == "user" and m.content == "hello there" for m in provider.calls[0]
    )


async def test_model_call_is_bracketed_by_start_and_done(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Symmetric with tool.start/tool.done: every model call logs a start and a
    # done (with token cost and duration) at INFO.
    agent = Agent(name="a", model=ScriptedProvider([text("hi")]))
    with caplog.at_level(logging.INFO, logger="lovia.runtime.loop"):
        await Runner.run(agent, "go")
    msgs = [r.message for r in caplog.records]
    assert any(m.startswith("model.start:") for m in msgs)
    done = [m for m in msgs if m.startswith("model.done:")]
    assert done and "tokens=" in done[0] and "dur=" in done[0]


async def test_max_turns_is_logged_once_at_the_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The run.interrupted boundary log is the single record for max-turns; the
    # old per-check run.max_turns line was redundant and removed.
    @tool
    async def noop() -> str:
        return "ok"

    agent = Agent(name="a", model=ScriptedProvider([call("noop", {})]), tools=[noop])
    with caplog.at_level(logging.WARNING, logger="lovia.runtime.loop"):
        with pytest.raises(MaxTurnsExceeded):
            await Runner.run(agent, "go", max_turns=1)
    msgs = [r.message for r in caplog.records]
    assert not any(m.startswith("run.max_turns:") for m in msgs)
    assert sum(m.startswith("run.interrupted:") for m in msgs) == 1


async def test_empty_final_message_completes_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # No content and no tool calls still completes the run (output "") — but
    # that is almost always a provider hiccup or max_tokens truncation, so a
    # run.empty_output warning must be left behind.
    from lovia.messages import AssistantTurn, Usage

    empty = AssistantTurn(content=None, usage=Usage(input_tokens=1, output_tokens=0))
    agent = Agent(name="a", model=ScriptedProvider([empty]))
    with caplog.at_level(logging.WARNING, logger="lovia.runtime.loop"):
        result = await Runner.run(agent, "go")
    assert result.output == ""
    assert any(r.message.startswith("run.empty_output:") for r in caplog.records)


async def test_finish_reason_surfaces_on_the_result() -> None:
    from lovia.messages import AssistantTurn, Usage

    turn = AssistantTurn(
        content="cut off mid-",
        usage=Usage(input_tokens=1, output_tokens=1),
        finish_reason="length",
    )
    agent = Agent(name="a", model=ScriptedProvider([turn]))
    result = await Runner.run(agent, "go")
    assert result.output == "cut off mid-"
    assert result.finish_reason == "length"


async def test_policy_mutating_request_entries_cannot_corrupt_the_transcript() -> None:
    # The context policy receives a defensive snapshot: a misbehaving policy
    # that mutates req.entries in place must not damage the live transcript
    # the Session and checkpoint persist.
    from lovia.context.policy import CompactionRequest, ContextResult

    class VandalPolicy:
        async def compact(self, req: CompactionRequest) -> ContextResult:
            req.entries.clear()  # contract violation, deliberately
            return ContextResult(entries=req.entries, changed=False)

    provider = ScriptedProvider([text("intact")])
    agent = Agent(name="a", model=provider)
    result = await Runner.run(agent, "hello", context_policy=VandalPolicy())

    assert result.output == "intact"
    # The model still saw the real transcript (changed=False -> live view)...
    assert any(m.role == "user" and m.content == "hello" for m in provider.calls[0])
    # ...and the run's own entries survived the vandalism.
    assert [type(e).__name__ for e in result.entries] == [
        "InputEntry",
        "AssistantTextEntry",
    ]
