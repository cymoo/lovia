"""Shared HTTP helpers for provider adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

import httpx

from ..exceptions import ContextOverflowError, ProviderError
from ._windows import window_from_error, window_from_models_payload


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
) -> None:
    """Raise a structured lovia exception for failed provider responses."""
    if response.status_code < 400:
        return

    body = await response.aread()
    text = body.decode(errors="replace")
    if is_context_overflow(response.status_code, text):
        raise ContextOverflowError(
            f"{label}: prompt exceeds the model's context window: {text}",
            reported_window=window_from_error(text),
        )
    raise ProviderError(
        f"{label} stream returned HTTP {response.status_code}: {text}",
        vendor=vendor,
        model=model,
        status_code=response.status_code,
        retryable=is_retryable_status(response.status_code),
        body=text,
    )


# The probe runs before the first model call, so its latency is charged to run
# start. The provider timeout (60s by default) is sized for generation, not for
# a metadata lookup that is pure upside: a slow endpoint should cost us a moment
# and then be forgotten, never stall the run.
_PROBE_TIMEOUT = 10.0


async def fetch_reported_window(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    model: str,
) -> int | None:
    """Ask ``GET {base_url}/models`` what ``model``'s context window is.

    Fails open: an unreachable endpoint, a slow one, an error status, a non-JSON
    body or a listing without window metadata all yield ``None``. The caller is
    trying to do better than "unknown", so nothing here is worth raising over.
    """
    try:
        response = await client.get(
            f"{base_url}/models",
            headers=headers,
            follow_redirects=True,
            timeout=_PROBE_TIMEOUT,
        )
        if not response.is_success:
            return None
        return window_from_models_payload(response.json(), model)
    except (httpx.HTTPError, ValueError):
        return None


def raise_for_transport_error(
    exc: httpx.TransportError,
    *,
    vendor: str,
    model: str | None,
    label: str,
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
        hint="Check network connectivity, proxy settings, provider base_url, and retry policy.",
    ) from exc
