"""Provider abstraction.

A :class:`Provider` is a thin async interface over a chat-completion-shaped
LLM endpoint. The runner only ever talks to providers through this protocol,
so adding support for a new vendor is a matter of writing one adapter class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from ..messages import AssistantMessage, ChatMessage


@dataclass
class ModelSettings:
    """Sampling parameters and other knobs forwarded to the provider.

    Only widely supported fields live here. Provider-specific settings can be
    passed via ``extra`` and forwarded as kwargs.

    ``cache_system`` is honored by providers that support prompt caching
    (currently Anthropic): when set, the adapter injects ``cache_control``
    breakpoints on the system prompt and the last tool definition so repeated
    runs with the same context are billed at the cached rate.
    """

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    parallel_tool_calls: bool | None = None
    cache_system: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamChunk:
    """A single chunk yielded by :meth:`Provider.stream`.

    Exactly one of ``text_delta``, ``reasoning_delta``, ``tool_call_delta`` or
    ``done`` is set on each chunk. ``done`` carries the fully assembled
    :class:`AssistantMessage`.
    """

    text_delta: str | None = None
    reasoning_delta: str | None = None
    tool_call_delta: "ToolCallDelta | None" = None
    done: AssistantMessage | None = None


@dataclass
class ToolCallDelta:
    """An incremental update to one tool call during streaming."""

    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str | None = None


@runtime_checkable
class Provider(Protocol):
    """The minimal interface every LLM backend must implement."""

    name: str

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AssistantMessage: ...

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[StreamChunk]: ...
