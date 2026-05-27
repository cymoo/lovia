"""Internal message representation.

The schema is intentionally isomorphic to the OpenAI Chat Completions wire
format. This keeps the OpenAI adapter trivial and gives every other provider a
single, well-understood target to translate to and from.

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

    def as_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class ChatMessage:
    """One message in a conversation.

    ``content`` may be a plain string (the common case), a list of typed
    :class:`ContentBlock`\\ s (for multimodal input like images), or ``None``
    when the assistant only emitted tool calls. ``tool_calls`` is only
    meaningful when ``role == "assistant"``; ``tool_call_id`` only when
    ``role == "tool"``. ``reasoning_content`` carries chain-of-thought text
    from providers that expose it (e.g. DeepSeek thinking, Anthropic
    extended thinking, OpenAI o-series) and must be echoed back verbatim in
    subsequent turns.
    """

    role: Role
    content: "str | list[ContentBlock] | None" = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # optional: agent/tool display name
    reasoning_content: str | None = None

    @property
    def text(self) -> str:
        """Flattened text view of :attr:`content` for logging / fallbacks."""
        return text_of(self.content)

    def as_openai(self) -> dict[str, Any]:
        """Serialize to the OpenAI Chat Completions wire format."""
        msg: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            msg["content"] = _content_to_openai(self.content)
        if self.reasoning_content is not None and self.role == "assistant":
            msg["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            msg["tool_calls"] = [tc.as_openai() for tc in self.tool_calls]
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        if self.name is not None and self.role in ("user", "assistant"):
            msg["name"] = self.name
        return msg


def _content_to_openai(
    content: "str | list[ContentBlock]",
) -> "str | list[dict[str, Any]]":
    """Serialize a message's content to the OpenAI Chat Completions wire format."""
    from .content import ImageBlock  # avoid cycle at module import

    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            if block.url is not None:
                image_url: dict[str, Any] = {"url": block.url}
            else:
                image_url = {"url": f"data:{block.mime_type};base64,{block.data}"}
            if block.detail is not None:
                image_url["detail"] = block.detail
            parts.append({"type": "image_url", "image_url": image_url})
        else:  # pragma: no cover - exhaustiveness guard
            raise TypeError(f"Unsupported content block: {block!r}")
    return parts


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
    """A model response returned by :class:`Provider.generate`.

    Streaming providers assemble this from chunks before returning it.
    ``reasoning_content`` is preserved when the provider returns chain-of-thought
    text that must be echoed back in subsequent requests.
    """

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    reasoning_content: str | None = None

    def to_chat_message(self) -> ChatMessage:
        return ChatMessage(
            role="assistant",
            content=self.content,
            tool_calls=list(self.tool_calls),
            reasoning_content=self.reasoning_content,
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
    return ChatMessage(role="user", content=list(content))


def _is_image_block(value: Any) -> bool:
    # Late import to avoid a hard cycle; ImageBlock lives in content.py.
    from .content import ImageBlock

    return isinstance(value, ImageBlock)


def assistant(text: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=text)


def tool_message(call_id: str, content: str) -> ChatMessage:
    return ChatMessage(role="tool", content=content, tool_call_id=call_id)
