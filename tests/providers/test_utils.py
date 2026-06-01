from __future__ import annotations

import httpx
import pytest

from lovia.exceptions import ContextOverflowError, ProviderError
from lovia.providers._http import raise_for_provider_status
from lovia.providers._sse import iter_sse_json


@pytest.mark.asyncio
async def test_provider_http_error_populates_metadata() -> None:
    response = httpx.Response(
        429,
        content=b"rate limited",
        request=httpx.Request("POST", "https://provider.test/v1"),
    )

    with pytest.raises(ProviderError) as exc_info:
        await raise_for_provider_status(
            response,
            vendor="fake",
            model="m1",
            label="Fake",
            is_context_overflow=lambda status, body: False,
        )

    exc = exc_info.value
    assert exc.vendor == "fake"
    assert exc.model == "m1"
    assert exc.status_code == 429
    assert exc.retryable is True
    assert exc.body == "rate limited"


@pytest.mark.asyncio
async def test_provider_http_error_marks_non_retryable_statuses() -> None:
    response = httpx.Response(
        401,
        content=b"bad key",
        request=httpx.Request("POST", "https://provider.test/v1"),
    )

    with pytest.raises(ProviderError) as exc_info:
        await raise_for_provider_status(
            response,
            vendor="fake",
            model="m1",
            label="Fake",
            is_context_overflow=lambda status, body: False,
        )

    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_provider_http_error_detects_context_overflow() -> None:
    response = httpx.Response(
        400,
        content=b"context too large",
        request=httpx.Request("POST", "https://provider.test/v1"),
    )

    with pytest.raises(ContextOverflowError):
        await raise_for_provider_status(
            response,
            vendor="fake",
            model="m1",
            label="Fake",
            is_context_overflow=lambda status, body: "context" in body,
        )


@pytest.mark.asyncio
async def test_iter_sse_json_skips_noise_and_stops_on_done() -> None:
    response = httpx.Response(
        200,
        content=(
            b"event: message\n"
            b'data: {"type": "one"}\n\n'
            b"data: not-json\n\n"
            b'data: {"type": "two"}\n\n'
            b"data: [DONE]\n\n"
            b'data: {"type": "ignored"}\n\n'
        ),
        request=httpx.Request("POST", "https://provider.test/v1"),
    )

    events = [event async for event in iter_sse_json(response)]

    assert events == [{"type": "one"}, {"type": "two"}]


@pytest.mark.asyncio
async def test_iter_sse_json_joins_multiline_data_and_flushes_eof() -> None:
    response = httpx.Response(
        200,
        content=(
            b"event: message\n"
            b'data: {"type":\n'
            b'data: "one"}\n\n'
            b"data: []\n\n"
            b'data: {"type": "tail"}\n'
        ),
        request=httpx.Request("POST", "https://provider.test/v1"),
    )

    events = [event async for event in iter_sse_json(response)]

    assert events == [{"type": "one"}, {"type": "tail"}]
