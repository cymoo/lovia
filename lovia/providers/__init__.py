"""Provider package exports.

Importing :func:`provider_from_string` lets users write::

    Agent(model="openai:gpt-5.5", ...)
    Agent(model="anthropic:claude-sonnet-4-5", ...)

while still allowing them to pass a :class:`Provider` instance directly.

Third-party packages can register additional vendor prefixes through the
``lovia.providers`` entry-point group — see
:func:`provider_from_string` for the contract. Entry points cannot shadow
the built-in prefixes (a safety property: installing a package never
silently reroutes ``openai:``/``anthropic:`` specs); overriding a built-in
requires an explicit :func:`register_provider` call.
"""

from __future__ import annotations

import logging
import os
from importlib.metadata import EntryPoint
from typing import Callable, cast

from ..exceptions import UserError
from .base import ModelSettings, Provider, supports_vision
from .anthropic import AnthropicProvider
from .openai_chat import OpenAIChatProvider

logger = logging.getLogger(__name__)

__all__ = [
    "ModelSettings",
    "Provider",
    "AnthropicProvider",
    "OpenAIChatProvider",
    "model_from_env",
    "provider_from_string",
    "register_provider",
    "supports_vision",
]


# Built-in vendor → factory map. Factories take the model string (the part
# after the ``vendor:`` prefix) and return a Provider instance. They should
# also accept optional ``api_key``/``base_url`` keyword overrides
# (``(model, *, api_key=None, base_url=None) -> Provider``); factories that
# don't are still usable as long as no overrides are requested.
ProviderFactory = Callable[..., Provider]

_BUILTIN: dict[str, ProviderFactory] = {
    "anthropic": lambda model, **kw: AnthropicProvider(model=model, **kw),
    "claude": lambda model, **kw: AnthropicProvider(model=model, **kw),
    "openai": lambda model, **kw: OpenAIChatProvider(model=model, **kw),
    "openai-chat": lambda model, **kw: OpenAIChatProvider(model=model, **kw),
    "oai": lambda model, **kw: OpenAIChatProvider(model=model, **kw),
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
    must return a :class:`Provider`. It should also accept optional
    ``api_key``/``base_url`` keyword overrides (see :data:`ProviderFactory`).
    Later registrations override earlier ones for the same prefix, including
    the built-in ``openai``/``anthropic`` prefixes.
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
        raise UserError(
            f"Provider plugin {vendor!r} failed to load from entry point "
            f"{ep.value!r}: {exc}",
            hint="The plugin package is installed but broken — check its import path and dependencies.",
        ) from exc
    if isinstance(obj, type):

        def _factory(model: str, _cls: type = obj, **kw: object) -> Provider:
            return _cls(model=model, **kw)  # type: ignore[no-any-return]

        return _factory
    if callable(obj):
        return cast(ProviderFactory, obj)
    raise UserError(
        f"Provider plugin {vendor!r} must be a provider class or callable factory"
    )


def provider_from_string(
    spec: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    supports_vision: bool | None = None,
) -> Provider:
    """Build a provider from a ``"<vendor>:<model>"`` string.

    Built-in prefixes: ``openai`` (aliases ``openai-chat``, ``oai``) and
    ``anthropic`` (alias ``claude``). Additional vendors can be plugged in via
    :func:`register_provider` or the
    ``lovia.providers`` entry-point group. A bare model name defaults to
    OpenAI Chat Completions (the intended path for OpenAI-compatible endpoints
    such as DeepSeek/Ollama/vLLM via ``OPENAI_BASE_URL``). A bare name that
    looks like an Anthropic model is almost certainly a missing ``anthropic:``
    prefix, so we log a warning rather than silently misroute it.

    ``api_key``/``base_url``/``supports_vision`` override the provider's
    environment- or host-derived defaults; a third-party factory that doesn't
    accept an override raises :class:`~lovia.UserError` (only when that
    override is actually given).
    """
    kwargs: dict[str, object] = {}
    if api_key is not None:
        kwargs["api_key"] = api_key
    if base_url is not None:
        kwargs["base_url"] = base_url
    if supports_vision is not None:
        kwargs["supports_vision"] = supports_vision
    if ":" not in spec:
        if spec.lower().startswith("claude"):
            logger.warning(
                "provider.no_vendor_prefix: model %r routed to the "
                "OpenAI-compatible provider; did you mean 'anthropic:%s'?",
                spec,
                spec,
            )
        # Passing None is identical to omitting: the constructor falls back
        # to the OPENAI_* environment variables.
        return OpenAIChatProvider(
            model=spec,
            api_key=api_key,
            base_url=base_url,
            supports_vision=supports_vision,
        )
    vendor, model = spec.split(":", 1)
    vendor = vendor.lower()
    # Explicit registrations win over builtins so applications can swap in
    # their own adapter for a built-in prefix.
    factory = _REGISTRY.get(vendor) or _BUILTIN.get(vendor)
    if factory is None:
        factory = _factory_from_entry_point(vendor)
        if factory is not None:
            _REGISTRY[vendor] = factory
    if factory is not None:
        if not kwargs:
            return factory(model)
        try:
            return factory(model, **kwargs)
        except TypeError as exc:
            raise UserError(
                f"provider plugin {vendor!r} does not accept "
                f"api_key/base_url/supports_vision overrides: {exc}"
            ) from exc
    raise UserError(
        f"Unknown model spec: {spec!r}",
        hint="Built-in prefixes: openai (aliases openai-chat, oai) and "
        "anthropic (alias claude). Register other vendors via "
        "lovia.providers.register_provider or the 'lovia.providers' "
        "entry-point group.",
    )


def model_from_env(*, required: bool = True) -> str | None:
    """Return the model id configured in the environment.

    Reads ``LOVIA_MODEL`` — the single knob for model selection. Its value is
    whatever ``Agent(model=...)`` accepts — usually ``"vendor:model"``; a bare
    id (no ``vendor:`` prefix) routes to the OpenAI-compatible provider, the
    intended path for ``OPENAI_BASE_URL`` services (DeepSeek, Ollama, vLLM,
    ...).

    With ``required=True`` (the default) a missing configuration raises
    :class:`~lovia.UserError` with a setup hint — the fail-loudly behavior
    scripts want. Pass ``required=False`` to get ``None`` instead and layer
    your own fallback (the web CLI does this to add its ``--model`` flag).
    """
    value = os.getenv("LOVIA_MODEL")
    if value:
        return value
    if required:
        raise UserError(
            "no model configured in the environment",
            hint='set LOVIA_MODEL (e.g. "openai:gpt-5.5" or '
            '"anthropic:claude-opus-4-8")',
        )
    return None
