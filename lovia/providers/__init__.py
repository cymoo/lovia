"""Provider package exports.

Importing :func:`provider_from_string` lets users write::

    Agent(model="openai:gpt-5.4", ...)
    Agent(model="anthropic:claude-4-5-sonnet", ...)

while still allowing them to pass a :class:`Provider` instance directly.

Third-party packages can register additional vendor prefixes through the
``lovia.providers`` entry-point group — see
:func:`provider_from_string` for the contract.
"""

from __future__ import annotations

import logging
from importlib.metadata import EntryPoint
from typing import Callable, cast

from .base import ModelSettings, Provider
from .anthropic import AnthropicProvider
from .openai_chat import OpenAIChatProvider

logger = logging.getLogger(__name__)

__all__ = [
    "ModelSettings",
    "Provider",
    "AnthropicProvider",
    "OpenAIChatProvider",
    "provider_from_string",
    "register_provider",
]


# Built-in vendor → factory map. Factories take the model string (the part
# after the ``vendor:`` prefix) and return a Provider instance.
ProviderFactory = Callable[[str], Provider]

_BUILTIN: dict[str, ProviderFactory] = {
    "anthropic": lambda model: AnthropicProvider(model=model),
    "claude": lambda model: AnthropicProvider(model=model),
    "openai": lambda model: OpenAIChatProvider(model=model),
    "openai-chat": lambda model: OpenAIChatProvider(model=model),
    "oai": lambda model: OpenAIChatProvider(model=model),
}


# Runtime registry (process-global). Third-party packages may add entries
# either by calling :func:`register_provider` at import time or — better —
# by declaring an entry point in the ``lovia.providers`` group, in which
# case discovery happens lazily on the first :func:`provider_from_string`
# call.
_REGISTRY: dict[str, ProviderFactory] = {}
_ENTRY_POINTS: dict[str, EntryPoint] | None = None


def register_provider(prefix: str, factory: ProviderFactory) -> None:
    """Register a vendor prefix → provider factory mapping.

    The factory receives the model string (everything after the colon) and
    must return a :class:`Provider`. Later registrations override earlier
    ones for the same prefix, including the built-in ``openai``/``anthropic``
    prefixes.
    """
    _REGISTRY[prefix.lower()] = factory


def _entry_points() -> dict[str, EntryPoint]:
    """Return provider entry points keyed by lower-case prefix."""

    global _ENTRY_POINTS
    if _ENTRY_POINTS is not None:
        return _ENTRY_POINTS
    from importlib.metadata import entry_points

    try:
        eps = entry_points(group="lovia.providers")
    except Exception:  # pragma: no cover - defensive
        _ENTRY_POINTS = {}
        return _ENTRY_POINTS
    _ENTRY_POINTS = {ep.name.lower(): ep for ep in eps}
    return _ENTRY_POINTS


def _factory_from_entry_point(vendor: str) -> ProviderFactory | None:
    ep = _entry_points().get(vendor)
    if ep is None:
        return None
    try:
        obj = ep.load()
    except Exception as exc:
        raise ValueError(
            f"Provider plugin {vendor!r} failed to load from entry point "
            f"{ep.value!r}: {exc}"
        ) from exc
    if isinstance(obj, type):

        def _factory(model: str, _cls: type = obj) -> Provider:
            return _cls(model=model)  # type: ignore[no-any-return]

        return _factory
    if callable(obj):
        return cast("Callable[[str], Provider]", obj)
    raise ValueError(
        f"Provider plugin {vendor!r} must be a provider class or callable factory"
    )


def provider_from_string(spec: str) -> Provider:
    """Build a provider from a ``"<vendor>:<model>"`` string.

    Built-in prefixes: ``openai`` (aliases ``openai-chat``, ``oai``) and
    ``anthropic`` (alias ``claude``). Additional vendors can be plugged in via
    :func:`register_provider` or the
    ``lovia.providers`` entry-point group. A bare model name defaults to
    OpenAI Chat Completions (the intended path for OpenAI-compatible endpoints
    such as DeepSeek/Ollama/vLLM via ``OPENAI_BASE_URL``). A bare name that
    looks like an Anthropic model is almost certainly a missing ``anthropic:``
    prefix, so we log a warning rather than silently misroute it.
    """
    if ":" not in spec:
        if spec.lower().startswith("claude"):
            logger.warning(
                "provider.no_vendor_prefix: model %r routed to the "
                "OpenAI-compatible provider; did you mean 'anthropic:%s'?",
                spec,
                spec,
            )
        return OpenAIChatProvider(model=spec)
    vendor, model = spec.split(":", 1)
    vendor = vendor.lower()
    # Explicit registrations win over builtins so applications can swap in
    # their own adapter for a built-in prefix.
    if vendor in _REGISTRY:
        return _REGISTRY[vendor](model)
    if vendor in _BUILTIN:
        return _BUILTIN[vendor](model)
    factory = _factory_from_entry_point(vendor)
    if factory is not None:
        _REGISTRY[vendor] = factory
        return factory(model)
    raise ValueError(
        f"Unknown model spec: {spec!r}. Built-in prefixes: openai, "
        f"anthropic. Register additional vendors via "
        f"lovia.providers.register_provider or the 'lovia.providers' "
        f"entry-point group."
    )
