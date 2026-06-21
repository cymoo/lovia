"""Entry-point / bootstrap behaviours of ``lovia.runtime.loop.RunLoop``.

The bulk of the loop is exercised by ``tests/runtime/test_runtime.py`` and the
end-to-end runner tests; this file pins the input-handling edges those don't:
session-id validation and seeding the transcript from a ``list[Message]``.
"""

from __future__ import annotations

import pytest

from lovia import Agent, Runner
from lovia.exceptions import UserError
from lovia.messages import Message
from lovia.stores import InMemorySession

from ..scripted_provider import ScriptedProvider, text


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
