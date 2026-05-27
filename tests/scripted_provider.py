"""A scripted in-memory provider used by the test suite.

Behaves like a real :class:`Provider` but reads its turn-by-turn responses
from a queue supplied at construction time. This lets us exercise the runner
loop deterministically without touching the network.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from lovia.messages import AssistantMessage, ChatMessage, ToolCall, Usage
from lovia.providers import StreamChunk
from lovia.providers.base import ModelSettings


class ScriptedProvider:
    """Replay a list of pre-canned :class:`AssistantMessage` answers."""

    name = "scripted"

    def __init__(self, script: list[AssistantMessage]) -> None:
        # ``calls`` records the messages received on each turn so tests can
        # assert on what the agent actually saw.
        self.calls: list[list[ChatMessage]] = []
        self._script = list(script)

    def _pop(self, messages: list[ChatMessage]) -> AssistantMessage:
        self.calls.append([_copy(m) for m in messages])
        if not self._script:
            raise AssertionError("ScriptedProvider ran out of canned responses")
        return self._script.pop(0)

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AssistantMessage:
        return self._pop(messages)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[StreamChunk]:
        msg = self._pop(messages)
        # Emit content character-by-character so streaming consumers receive
        # multiple deltas, then the final assembled message.
        if msg.content:
            for ch in msg.content:
                yield StreamChunk(text_delta=ch)
        yield StreamChunk(done=msg)


def text(content: str) -> AssistantMessage:
    return AssistantMessage(
        content=content, usage=Usage(input_tokens=1, output_tokens=1)
    )


def call(
    name: str, args: dict[str, Any], *, call_id: str | None = None
) -> AssistantMessage:
    return AssistantMessage(
        content=None,
        tool_calls=[
            ToolCall(
                id=call_id or f"call_{name}", name=name, arguments=json.dumps(args)
            )
        ],
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def _copy(m: ChatMessage) -> ChatMessage:
    return ChatMessage(
        role=m.role,
        content=m.content,
        tool_calls=list(m.tool_calls),
        tool_call_id=m.tool_call_id,
        name=m.name,
    )
