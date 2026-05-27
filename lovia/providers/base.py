"""Provider abstraction.

A :class:`Provider` is a thin async interface over a streaming chat-completion
LLM endpoint. The runner only ever talks to providers through this protocol,
so adding support for a new vendor is a matter of writing one adapter class.

Providers yield a stream of :class:`ItemDelta` values
(:class:`TextDelta` / :class:`ReasoningDelta` / :class:`ToolCallDelta` /
:class:`UsageDelta` / :class:`FinishDelta`) — the runner assembles them into
:class:`Item`\\ s and the final assistant turn. There is no non-streaming
``generate`` method; non-streaming clients can still buffer the stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from ..items import ItemDelta
from ..messages import ChatMessage


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


@runtime_checkable
class Provider(Protocol):
    """The minimal interface every LLM backend must implement."""

    name: str

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[ItemDelta]: ...
