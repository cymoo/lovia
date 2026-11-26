"""Shared HTTP helpers for provider adapters.

Deliberately free of domain knowledge: :mod:`._windows` builds on these, not
the other way round, so what an error body *means* stays in one place.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

import httpx

from ..exceptions import ContextOverflowError, ProviderError
from ..http_config import proxy_note

# What a proxy answers when it cannot reach (or will not relay to) the host.
_PROXY_STATUSES = frozenset({407, 502, 504})


def is_retryable_status(status_code: int) -> bool:
    """Return whether an HTTP status is likely worth retrying."""
    return status_code in (408, 429) or 500 <= status_code < 600


def host_matches(host: str | None, domains: tuple[str, ...]) -> bool:
    """True when ``host`` is one of ``domains`` or a subdomain of one.

    Subdomain matching keeps regional hosts (``eu.api.openai.com``) on the
    same behavior as their apex without letting lookalike domains
    (``evilapi.openai.com``) slip through.
    """
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


async def raise_for_provider_status(
    response: httpx.Response,
    *,
    vendor: str,
    model: str | None,
    label: str,
    is_context_overflow: Callable[[int, str], bool],
    window_from_body: Callable[[str], int | None] | None = None,
    trust_env: bool | None = None,
) -> None:
    """Raise a structured lovia exception for failed provider responses.

    ``window_from_body`` reads the context window an overflow names, when it
    names one; the adapters pass
    :func:`~lovia.providers._windows.window_from_error`. ``trust_env`` is
    the client's, ``None`` when unknown: with it on, a gateway status may be
    the proxy's own answer, and the error says so.
    """
    if response.status_code < 400:
        return

    body = await response.aread()
    text = body.decode(errors="replace")
    if is_context_overflow(response.status_code, text):
        raise ContextOverflowError(
            f"{label}: prompt exceeds the model's context window: {text}",
            reported_window=window_from_body(text) if window_from_body else None,
        )
    note = None
    if trust_env and response.status_code in _PROXY_STATUSES:
        note = proxy_note(str(response.request.url), trust_env)
    raise ProviderError(
        f"{label} stream returned HTTP {response.status_code}: {text}",
        vendor=vendor,
        model=model,
        status_code=response.status_code,
        retryable=is_retryable_status(response.status_code),
        body=text,
        hint=note,
    )


def proxy_hint(base: str, url: str, trust_env: bool | None) -> str:
    """``base``, plus how the proxy settings bore on the request to ``url``.
    ``trust_env`` is ``None`` for a client the caller built: its settings
    are not ours to describe."""
    note = proxy_note(url, trust_env) if trust_env is not None else None
    return f"{base} {note}" if note else base


def raise_for_transport_error(
    exc: httpx.TransportError,
    *,
    vendor: str,
    model: str | None,
    label: str,
    url: str = "",
    trust_env: bool | None = None,
) -> NoReturn:
    """Translate network-layer failures into structured provider errors."""
    # RemoteProtocolError is how a mid-stream disconnect surfaces (gateway or
    # LB dropping an SSE response); it is as transient as a network error.
    # LocalProtocolError stays non-retryable: we built a bad request.
    retryable = isinstance(
        exc,
        httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError,
    )
    raise ProviderError(
        f"{label} stream failed before the provider returned a complete response: {exc}",
        vendor=vendor,
        model=model,
        retryable=retryable,
        hint=proxy_hint(
            "Check network connectivity, proxy settings, provider base_url, and retry policy.",
            url,
            trust_env,
        ),
    ) from exc
