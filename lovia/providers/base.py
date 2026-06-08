"""Provider abstraction.

A :class:`Provider` is a thin async interface over a streaming chat-completion
LLM endpoint. The runner only ever talks to providers through this protocol,
so adding support for a new vendor is a matter of writing one adapter class.

Providers yield a stream of :class:`ModelDelta` values. Display deltas keep UI
latency low, and :class:`EntryCompletedDelta` lets adapters hand the runner a
final provider-native transcript entry when ids or metadata must be preserved.

Providers MAY additionally implement two optional methods used by
:class:`~lovia.ContextPolicy`:

* ``estimate_tokens(entries) -> int`` — approximate prompt size; the framework
  falls back to a chars/4 heuristic via :func:`estimate_tokens` below.
* ``context_window(model) -> int | None`` — the maximum prompt+output tokens
  the named model accepts; ``None`` (or absent method) means "unknown".

Neither method is required by the Protocol so existing adapters keep working;
:func:`estimate_tokens` and :func:`context_window` below dispatch to the
adapter when available and fall back otherwise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

from .._types import JsonObject
from ..transcript import TranscriptEntry, ModelDelta, entry_to_dict


@dataclass
class ModelSettings:
    """Sampling parameters and other knobs forwarded to the provider.

    Only widely supported fields live here. Provider-specific settings belong
    in ``provider_options`` under the adapter's provider key, which prevents
    fallback chains from leaking vendor-only knobs across providers.
    """

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    parallel_tool_calls: bool | None = None
    provider_options: dict[str, JsonObject] = field(default_factory=dict)


def provider_options(settings: ModelSettings, *keys: str) -> JsonObject:
    """Return a merged copy of provider-specific settings for ``keys``."""

    out: JsonObject = {}
    for key in keys:
        out.update(settings.provider_options.get(key, {}))
    return out


@runtime_checkable
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
    def context_window(self, model: str) -> int | None: ...


def estimate_tokens(provider: object, entries: list[TranscriptEntry]) -> int:
    """Approximate the prompt size of ``entries`` for ``provider``.

    If the provider exposes an ``estimate_tokens(entries) -> int`` method we
    defer to it (vendors who ship a tokenizer should override). Otherwise
    we fall back to a deliberately rough chars / 4 heuristic on the JSON
    serialization — enough to drive a "compact at 80% of the window"
    policy without pulling in tiktoken as a hard dependency.
    """
    if isinstance(provider, TokenEstimator):
        return int(provider.estimate_tokens(entries))
    # ``entry_to_dict`` is cheap and already used by sessions; reusing it
    # here keeps the estimate consistent with what gets persisted.
    chars = sum(
        len(json.dumps(entry_to_dict(it), ensure_ascii=False)) for it in entries
    )
    # //4 is the textbook GPT heuristic; close enough for thresholding.
    return chars // 4


def context_window(provider: object, model: str | None) -> int | None:
    """Return the prompt+output token cap for ``model`` on ``provider``.

    Returns ``None`` when the provider doesn't expose the information (no
    ``context_window`` method, or the method returns ``None``). Callers
    treat ``None`` as "skip proactive compaction; rely on the reactive
    overflow path instead".
    """
    if model is None:
        return None
    if isinstance(provider, ContextWindowProvider):
        result = provider.context_window(model)
        return int(result) if result is not None else None
    return None
