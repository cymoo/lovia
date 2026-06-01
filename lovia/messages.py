"""Chat-compatible message representation.

The schema intentionally models the portable subset of chat transcripts.
Provider-native state such as reasoning ids, signatures, or encrypted blobs
lives in :mod:`lovia.items`, not here.

All messages carry a ``role`` and ``content``; assistant messages may also
carry ``tool_calls``; tool messages carry the ``tool_call_id`` they answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .content import ContentBlock, TextBlock, text_of


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
class ChatMessage:
    """One message in a conversation.

    ``content`` may be a plain string (the common case), a list of typed
    :class:`ContentBlock`\\ s (for multimodal input like images), or ``None``
    when the assistant only emitted tool calls. ``tool_calls`` is only
    meaningful when ``role == "assistant"``; ``tool_call_id`` only when
    ``role == "tool"``.
    """

    role: Role
    content: "str | list[ContentBlock] | None" = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # optional: agent/tool display name

    @property
    def text(self) -> str:
        """Flattened text view of :attr:`content` for logging / fallbacks."""
        return text_of(self.content)


@dataclass
class Usage:
    """Token usage for one or more model calls."""

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


@dataclass
class AssistantMessage:
    """Chat-compatible view of one assistant turn."""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None

    def to_chat_message(self) -> ChatMessage:
        return ChatMessage(
            role="assistant",
            content=self.content,
            tool_calls=list(self.tool_calls),
        )


def system(text: str) -> ChatMessage:
    return ChatMessage(role="system", content=text)


def user(
    content: "str | ContentBlock | list[ContentBlock]",
) -> ChatMessage:
    """Build a user message from a string, a single block, or a block list."""
    if isinstance(content, str):
        return ChatMessage(role="user", content=content)
    if isinstance(content, (TextBlock,)) or _is_image_block(content):
        return ChatMessage(role="user", content=[content])  # type: ignore[list-item]
    return ChatMessage(role="user", content=list(content))  # type: ignore[arg-type]


def _is_image_block(value: Any) -> bool:
    # Late import to avoid a hard cycle; ImageBlock lives in content.py.
    from .content import ImageBlock

    return isinstance(value, ImageBlock)


def assistant(text: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=text)


def tool_message(call_id: str, content: str) -> ChatMessage:
    return ChatMessage(role="tool", content=content, tool_call_id=call_id)
