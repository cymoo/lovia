"""Shared HTTP helpers for provider adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

import httpx

from ..exceptions import ContextOverflowError, ProviderError


def is_retryable_status(status_code: int) -> bool:
    """Return whether an HTTP status is likely worth retrying."""
    return status_code in (408, 429) or 500 <= status_code < 600


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
            f"{label}: prompt exceeds the model's context window: {text}"
        )
    raise ProviderError(
        f"{label} stream returned HTTP {response.status_code}: {text}",
        vendor=vendor,
        model=model,
        status_code=response.status_code,
        retryable=is_retryable_status(response.status_code),
        body=text,
    )


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
