"""Provider package exports.

Importing :func:`provider_from_string` lets users write::

    Agent(model="openai:gpt-4o-mini", ...)
    Agent(model="anthropic:claude-3-5-sonnet-latest", ...)

while still allowing them to pass a :class:`Provider` instance directly.
"""

from __future__ import annotations

from .base import ModelSettings, Provider, StreamChunk, ToolCallDelta
from .openai_chat import OpenAIChatProvider

__all__ = [
    "ModelSettings",
    "Provider",
    "StreamChunk",
    "ToolCallDelta",
    "OpenAIChatProvider",
    "provider_from_string",
]


def provider_from_string(spec: str) -> Provider:
    """Build a provider from a ``"<vendor>:<model>"`` string.

    Recognised prefixes: ``openai:``, ``anthropic:``. A bare model name with
    no prefix defaults to OpenAI Chat Completions.
    """
    if ":" not in spec:
        return OpenAIChatProvider(model=spec)
    vendor, model = spec.split(":", 1)
    vendor = vendor.lower()
    if vendor in ("openai", "oai"):
        return OpenAIChatProvider(model=model)
    if vendor in ("anthropic", "claude"):
        # Imported lazily to avoid the httpx-only install pulling Anthropic in.
        from .anthropic import AnthropicProvider

        return AnthropicProvider(model=model)
    raise ValueError(f"Unknown model spec: {spec!r}")
