"""A scripted in-memory provider used by the test suite.

Behaves like a real :class:`Provider` but reads its turn-by-turn responses
from a queue supplied at construction time. This lets us exercise the runner
loop deterministically without touching the network.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from lovia.items import (
    FinishDelta,
    ItemDelta,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
from lovia.messages import AssistantMessage, ChatMessage, ToolCall, Usage
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

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[ItemDelta]:
        msg = self._pop(messages)
        # Reasoning streams first (matches Anthropic / Responses ordering).
        if msg.reasoning_content:
            for ch in msg.reasoning_content:
                yield ReasoningDelta(text=ch)
        # Text streams character-by-character so consumers see multiple deltas.
        if msg.content:
            for ch in msg.content:
                yield TextDelta(text=ch)
        # Tool calls: emit the full assembled call as a single delta per index.
        for idx, tc in enumerate(msg.tool_calls):
            yield ToolCallDelta(
                index=idx, call_id=tc.id, name=tc.name, arguments=tc.arguments
            )
        yield UsageDelta(usage=msg.usage)
        yield FinishDelta(reason=msg.finish_reason)


def text(content: str, *, reasoning: str | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        reasoning_content=reasoning,
        usage=Usage(input_tokens=1, output_tokens=1),
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
