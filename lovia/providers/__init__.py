"""Provider package exports.

Importing :func:`provider_from_string` lets users write::

    Agent(model="openai:gpt-4o-mini", ...)
    Agent(model="anthropic:claude-3-5-sonnet-latest", ...)

while still allowing them to pass a :class:`Provider` instance directly.

Third-party packages can register additional vendor prefixes through the
``lovia.providers`` entry-point group — see
:func:`provider_from_string` for the contract.
"""

from __future__ import annotations

from typing import Callable

from .base import ModelSettings, Provider
from .openai_chat import OpenAIChatProvider

__all__ = [
    "ModelSettings",
    "Provider",
    "OpenAIChatProvider",
    "provider_from_string",
    "register_provider",
]


# Built-in vendor → factory map. Factories take the model string (the part
# after the ``vendor:`` prefix) and return a Provider instance.
ProviderFactory = Callable[[str], Provider]

_BUILTIN: dict[str, ProviderFactory] = {
    "openai": lambda model: OpenAIChatProvider(model=model),
    "oai": lambda model: OpenAIChatProvider(model=model),
}


def _anthropic_factory(model: str) -> Provider:
    # Imported lazily to avoid the httpx-only install pulling Anthropic in.
    from .anthropic import AnthropicProvider

    return AnthropicProvider(model=model)


_BUILTIN["anthropic"] = _anthropic_factory
_BUILTIN["claude"] = _anthropic_factory


def _openai_responses_factory(model: str) -> Provider:
    from .openai_responses import OpenAIResponsesProvider

    return OpenAIResponsesProvider(model=model)


_BUILTIN["openai-responses"] = _openai_responses_factory
_BUILTIN["responses"] = _openai_responses_factory


# Runtime registry (process-global). Third-party packages may add entries
# either by calling :func:`register_provider` at import time or — better —
# by declaring an entry point in the ``lovia.providers`` group, in which
# case discovery happens lazily on the first :func:`provider_from_string`
# call.
_REGISTRY: dict[str, ProviderFactory] = {}
_entry_points_loaded = False


def register_provider(prefix: str, factory: ProviderFactory) -> None:
    """Register a vendor prefix → provider factory mapping.

    The factory receives the model string (everything after the colon) and
    must return a :class:`Provider`. Later registrations override earlier
    ones for the same prefix.
    """
    _REGISTRY[prefix.lower()] = factory


def _load_entry_points() -> None:
    """Discover providers exposed via the ``lovia.providers`` entry point.

    Each entry point's name becomes the vendor prefix; the loaded object
    must be either a :class:`Provider` subclass (constructed as
    ``cls(model=model)``) or a callable matching :data:`ProviderFactory`.
    Failures are swallowed silently — a broken third-party plugin should
    not break model resolution for unrelated prefixes.
    """
    global _entry_points_loaded
    if _entry_points_loaded:
        return
    _entry_points_loaded = True
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - Python <3.10 doesn't ship here
        return
    try:
        eps = entry_points(group="lovia.providers")
    except Exception:  # pragma: no cover - defensive
        return
    for ep in eps:
        try:
            obj = ep.load()
        except Exception:  # pragma: no cover - broken plugin, skip
            continue
        if isinstance(obj, type):
            # A concrete Provider class — instantiate with ``model=...``.
            def _factory(model: str, _cls: type = obj) -> Provider:
                return _cls(model=model)  # type: ignore[no-any-return]

            _REGISTRY.setdefault(ep.name.lower(), _factory)
        elif callable(obj):
            _REGISTRY.setdefault(ep.name.lower(), obj)


def provider_from_string(spec: str) -> Provider:
    """Build a provider from a ``"<vendor>:<model>"`` string.

    Built-in prefixes: ``openai`` (alias ``oai``), ``openai-responses``
    (alias ``responses``), ``anthropic`` (alias ``claude``). Additional
    vendors can be plugged in via :func:`register_provider` or the
    ``lovia.providers`` entry-point group. A bare model name with no
    prefix defaults to OpenAI Chat Completions.
    """
    if ":" not in spec:
        return OpenAIChatProvider(model=spec)
    vendor, model = spec.split(":", 1)
    vendor = vendor.lower()
    if vendor in _BUILTIN:
        return _BUILTIN[vendor](model)
    # Lazily import third-party plugins on first miss.
    _load_entry_points()
    if vendor in _REGISTRY:
        return _REGISTRY[vendor](model)
    raise ValueError(
        f"Unknown model spec: {spec!r}. Built-in prefixes: openai, "
        f"openai-responses, anthropic. Register additional vendors via "
        f"lovia.providers.register_provider or the 'lovia.providers' "
        f"entry-point group."
    )
