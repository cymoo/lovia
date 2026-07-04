"""Deterministic test doubles for agents, tests, and evals.

:class:`ScriptedProvider` behaves like a real :class:`~lovia.Provider` but
replays pre-canned responses from a queue, so agent behavior can be exercised
offline, free, and reproducibly::

    from lovia import Agent
    from lovia.testing import ScriptedProvider, call, text

    agent = Agent(
        name="bot",
        model=ScriptedProvider([call("search", {"q": "tides"}), text("Done.")]),
    )

A scripted provider **pops a shared script** — it is neither repeat- nor
concurrency-safe. Build a fresh instance (and agent) per run; with
:func:`lovia.eval.evaluate` pass an agent *factory* for exactly this reason.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from .messages import AssistantTurn, Message, ToolCall, Usage
from .providers.base import ModelSettings
from .transcript import (
    EntryCompletedDelta,
    FinishDelta,
    ModelDelta,
    ReasoningDelta,
    ReasoningEntry,
    TextDelta,
    ToolCallDelta,
    TranscriptEntry,
    UsageDelta,
    entries_to_messages,
)


class ScriptedProvider:
    """Replay a list of pre-canned :class:`AssistantTurn` answers."""

    name = "scripted"
    model: str | None = None
    supports_json_schema = False

    def __init__(self, script: list[AssistantTurn]) -> None:
        # ``calls`` records the flattened Message form received on each
        # turn so tests can assert on what the agent actually saw. We accept
        # the new TranscriptEntry-based interface but flatten internally for
        # backwards test ergonomics.
        self.calls: list[list[Message]] = []
        self._script = list(script)

    def _pop(self, input: list[TranscriptEntry]) -> AssistantTurn:
        messages = entries_to_messages(input)
        self.calls.append([_copy(m) for m in messages])
        if not self._script:
            raise AssertionError("ScriptedProvider ran out of canned responses")
        return self._script.pop(0)

    async def stream(
        self,
        entries: list[TranscriptEntry],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[ModelDelta]:
        msg = self._pop(entries)
        # Reasoning streams first (matches Anthropic ordering).
        reasoning = getattr(msg, "_scripted_reasoning_content", None)
        if reasoning:
            for ch in reasoning:
                yield ReasoningDelta(text=ch)
            yield EntryCompletedDelta(
                ReasoningEntry(content=reasoning, provider=self.name)
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


def text(content: str, *, reasoning: str | None = None) -> AssistantTurn:
    """A scripted turn that answers with plain text."""
    msg = AssistantTurn(
        content=content,
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    if reasoning is not None:
        setattr(msg, "_scripted_reasoning_content", reasoning)
    return msg


def call(
    name: str, args: dict[str, Any], *, call_id: str | None = None
) -> AssistantTurn:
    """A scripted turn that requests one tool call."""
    return AssistantTurn(
        content=None,
        tool_calls=[
            ToolCall(
                id=call_id or f"call_{name}", name=name, arguments=json.dumps(args)
            )
        ],
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def batch(
    *specs: "tuple[str, dict[str, Any]] | tuple[str, dict[str, Any], str]",
) -> AssistantTurn:
    """A scripted turn that requests several tool calls at once.

    Each spec is ``(name, args)`` or ``(name, args, call_id)``; ids default to
    ``call_<index>_<name>`` so duplicate tool names stay distinct.
    """
    tool_calls = []
    for idx, spec in enumerate(specs):
        name, args = spec[0], spec[1]
        call_id = spec[2] if len(spec) == 3 else f"call_{idx}_{name}"
        tool_calls.append(ToolCall(id=call_id, name=name, arguments=json.dumps(args)))
    return AssistantTurn(
        content=None,
        tool_calls=tool_calls,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def _copy(m: Message) -> Message:
    return Message(
        role=m.role,
        content=m.content,
        tool_calls=list(m.tool_calls),
        tool_call_id=m.tool_call_id,
        name=m.name,
    )


__all__ = ["ScriptedProvider", "batch", "call", "text"]
