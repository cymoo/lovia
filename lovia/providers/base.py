"""Provider abstraction.

A :class:`Provider` is a thin async interface over a streaming chat-completion
LLM endpoint. The runner only ever talks to providers through this protocol,
so adding support for a new vendor is a matter of writing one adapter class.

Providers yield a stream of :class:`ModelDelta` values. Display deltas keep UI
latency low, and :class:`EntryCompletedDelta` lets adapters hand the runner a
final provider-native transcript entry when ids or metadata must be preserved.

Providers MAY additionally implement three optional methods used by
:class:`~lovia.ContextPolicy`:

* ``estimate_tokens(entries) -> int`` — approximate prompt size; without it
  the context layer's :class:`~lovia.context.TokenCounter` falls back to its
  chars/4 heuristic.
* ``context_window() -> int | None`` — the maximum prompt+output tokens this
  provider's model accepts; ``None`` (or absent method) means "unknown". Purely
  local: no I/O, safe to call per turn.
* ``async discover_context_window() -> int | None`` — ask the *endpoint* what
  the window is. The runner calls this once, before the first model call; the
  adapter decides whether that costs a request, and caches the answer.

None is required, and a provider that implements none simply gets the
heuristics. But the Protocols are ``runtime_checkable``, which only checks that
a method *exists* — so an adapter that implements one with the wrong signature
fails at call time, not at registration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

from ..types import JsonObject
from ..transcript import TranscriptEntry, ModelDelta


@dataclass
class ModelSettings:
    """Sampling parameters and other knobs forwarded to the provider.

    Only widely supported fields live here; on every field ``None`` means
    "don't send it — let the provider apply its own default".
    """

    temperature: float | None = None
    """Sampling temperature; higher is more random."""

    top_p: float | None = None
    """Nucleus-sampling probability mass."""

    max_tokens: int | None = None
    """Cap on output tokens per model response."""

    stop: list[str] | None = None
    """Sequences at which the model stops generating."""

    parallel_tool_calls: bool | None = None
    """Whether the model may request several tool calls in one turn."""

    provider_options: dict[str, JsonObject] = field(default_factory=dict)
    """Vendor-only knobs, keyed by adapter name (``"openai"``,
    ``"anthropic"``, ...). Keys pass through to that adapter's wire payload —
    a fallback chain never leaks one vendor's knobs into another's request.
    A ``None`` value strips an adapter default the endpoint rejects."""


def provider_options(settings: ModelSettings, *keys: str) -> JsonObject:
    """Return a merged copy of provider-specific settings for ``keys``.

    Later keys override earlier ones, so adapters pass their canonical name
    last. A ``None`` value is an explicit removal: adapters drop None-valued
    fields from the final payload, letting users strip an adapter default
    (e.g. ``{"stream_options": None}`` for endpoints that reject it).
    """

    out: JsonObject = {}
    for key in keys:
        out.update(settings.provider_options.get(key, {}))
    return out


class Provider(Protocol):
    """The minimal interface every LLM backend must implement.

    Providers consume :class:`TranscriptEntry` lists — the framework's vendor-neutral
    transcript form — and emit :class:`ModelDelta` values as the model streams.
    Chat-style adapters (OpenAI Chat, Anthropic) flatten incoming entries to
    their wire ``messages`` shape internally while preserving richer state in
    ``EntryCompletedDelta`` values when a provider exposes it.
    """

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str | None: ...

    @property
    def supports_json_schema(self) -> bool: ...

    def stream(
        self,
        entries: list[TranscriptEntry],
        *,
        tools: list[JsonObject] | None = None,
        response_format: JsonObject | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[ModelDelta]: ...


# ---------------------------------------------------------------------------
# Context-window helpers used by ContextPolicy
# ---------------------------------------------------------------------------


@runtime_checkable
class TokenEstimator(Protocol):
    def estimate_tokens(self, entries: list[TranscriptEntry]) -> int: ...


@runtime_checkable
class ContextWindowProvider(Protocol):
    def context_window(self) -> int | None: ...


@runtime_checkable
class ContextWindowDiscovery(Protocol):
    async def discover_context_window(self) -> int | None: ...


def context_window(provider: object) -> int | None:
    """Return the prompt+output token cap ``provider`` will accept.

    A provider already knows which model it speaks to — ``stream`` takes no
    model either — so this asks about *that* model. Returns ``None`` when the
    provider doesn't expose the information (no ``context_window`` method, or
    the method returns ``None``). Callers treat ``None`` as "skip proactive
    compaction; rely on the reactive overflow path instead".
    """
    if isinstance(provider, ContextWindowProvider):
        result = provider.context_window()
        return int(result) if result is not None else None
    return None


async def discover_context_window(provider: object) -> int | None:
    """Ask ``provider`` to look its context window up at the endpoint.

    Separate from :func:`context_window` — and deliberately ``async`` — because
    this one can make a network request. It is a one-shot warm-up the runner
    performs before the first model call, never something a policy triggers
    from inside a hot path. Adapters cache the answer (a miss included) and
    never raise, so calling this is always safe and at most costs one request.
    """
    if isinstance(provider, ContextWindowDiscovery):
        result = await provider.discover_context_window()
        return int(result) if result is not None else None
    return None
