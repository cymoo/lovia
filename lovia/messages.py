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

    ``content`` is optional because assistant messages that only call tools
    carry no textual content. ``tool_calls`` is only meaningful when
    ``role == "assistant"``; ``tool_call_id`` only when ``role == "tool"``.
    ``reasoning_content`` carries chain-of-thought text from providers that
    expose it (e.g. DeepSeek thinking mode) and must be echoed back verbatim
    in subsequent turns.
    """

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # optional: agent/tool display name
    reasoning_content: str | None = None

    def as_openai(self) -> dict[str, Any]:
        """Serialize to the OpenAI Chat Completions wire format."""
        msg: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            msg["content"] = self.content
        if self.reasoning_content is not None and self.role == "assistant":
            msg["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            msg["tool_calls"] = [tc.as_openai() for tc in self.tool_calls]
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        if self.name is not None and self.role in ("user", "assistant"):
            msg["name"] = self.name
        return msg


@dataclass
class Usage:
    """Token usage for one or more model calls."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


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


def user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text)


def assistant(text: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=text)


def tool_message(call_id: str, content: str) -> ChatMessage:
    return ChatMessage(role="tool", content=content, tool_call_id=call_id)
