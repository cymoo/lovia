"""Chat-compatible message representation.

The schema intentionally models the portable subset of chat transcripts.
Provider-native state such as reasoning ids, signatures, or encrypted blobs
lives in :mod:`lovia.transcript`, not here.

All messages carry a ``role`` and ``content``; assistant messages may also
carry ``tool_calls``; tool messages carry the ``tool_call_id`` they answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .parts import ContentPart, normalize_content, text_of

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    """A single tool invocation requested by the assistant."""

    id: str
    name: str
    # Raw JSON string as emitted by the model. Parsed lazily by the runner so
    # we can show the original payload in errors.
    arguments: str


@dataclass
class Message:
    """One message in a conversation.

    ``content`` may be a plain string (the common case), a list of typed
    :class:`ContentPart`\\ s (for multimodal input like images or files), or
    ``None`` when the assistant only emitted tool calls. ``tool_calls`` is only
    meaningful when ``role == "assistant"``; ``tool_call_id`` only when
    ``role == "tool"``.
    """

    role: Role
    content: "str | list[ContentPart] | None" = None
    reasoning: str | None = None  # chain-of-thought text for the web UI
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # optional: agent/tool display name

    @property
    def text(self) -> str:
        """Flattened text view of :attr:`content` for logging / fallbacks."""
        return text_of(self.content)


@dataclass
class Usage:
    """Token usage for one or more model calls.

    ``input_tokens`` is the **full** prompt size, including tokens served from
    or written to a provider prompt cache — adapters normalize to this
    (OpenAI's ``prompt_tokens`` already includes cached tokens; the Anthropic
    adapter adds its separately-reported cache counts back in). The cache
    fields break that total down for cost accounting.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    # Provider-reported cache statistics (Anthropic returns
    # ``cache_creation_input_tokens`` and ``cache_read_input_tokens``; OpenAI
    # surfaces ``cached_tokens``). The exact accounting differs per vendor but
    # the meaning is the same: input tokens served from cache.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    def clone(self) -> Usage:
        """Return an independent copy (``Usage`` is mutated in place by ``add``)."""
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
        )


@dataclass
class AssistantTurn:
    """Provider-agnostic result of one assistant turn."""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None

    def to_message(self) -> Message:
        return Message(
            role="assistant",
            content=self.content,
            tool_calls=list(self.tool_calls),
        )


def system(text: str) -> Message:
    return Message(role="system", content=text)


def user(
    content: "str | ContentPart | list[ContentPart]",
) -> Message:
    """Build a user message from a string, a single part, or a part list."""
    return Message(role="user", content=normalize_content(content))


def assistant(text: str) -> Message:
    return Message(role="assistant", content=text)


def tool_message(call_id: str, content: str) -> Message:
    return Message(role="tool", content=content, tool_call_id=call_id)


__all__ = [
    "AssistantTurn",
    "Message",
    "Role",
    "ToolCall",
    "Usage",
    "assistant",
    "system",
    "tool_message",
    "user",
]
