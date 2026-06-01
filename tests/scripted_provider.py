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
    Item,
    ItemCompletedDelta,
    ItemDelta,
    ReasoningDelta,
    ReasoningItem,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
    items_to_chat_messages,
)
from lovia.messages import AssistantMessage, ChatMessage, ToolCall, Usage
from lovia.providers.base import ModelSettings


class ScriptedProvider:
    """Replay a list of pre-canned :class:`AssistantMessage` answers."""

    name = "scripted"
    supports_json_schema = False

    def __init__(self, script: list[AssistantMessage]) -> None:
        # ``calls`` records the flattened ChatMessage form received on each
        # turn so tests can assert on what the agent actually saw. We accept
        # the new Item-based interface but flatten internally for backwards
        # test ergonomics.
        self.calls: list[list[ChatMessage]] = []
        self._script = list(script)

    def _pop(self, input: list[Item]) -> AssistantMessage:
        messages = items_to_chat_messages(input)
        self.calls.append([_copy(m) for m in messages])
        if not self._script:
            raise AssertionError("ScriptedProvider ran out of canned responses")
        return self._script.pop(0)

    async def stream(
        self,
        input: list[Item],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[ItemDelta]:
        msg = self._pop(input)
        # Reasoning streams first (matches Anthropic ordering).
        reasoning = getattr(msg, "_scripted_reasoning_content", None)
        if reasoning:
            for ch in reasoning:
                yield ReasoningDelta(text=ch)
            yield ItemCompletedDelta(
                ReasoningItem(content=reasoning, provider=self.name)
            )
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
    msg = AssistantMessage(
        content=content,
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    if reasoning is not None:
        setattr(msg, "_scripted_reasoning_content", reasoning)
    return msg


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
