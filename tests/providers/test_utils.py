from __future__ import annotations

import httpx
import pytest

from lovia.exceptions import ContextOverflowError, ProviderError
from lovia.providers._http import (
    host_matches,
    is_retryable_status,
    raise_for_provider_status,
    raise_for_transport_error,
)
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


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(408, True), (429, True), (500, True), (503, True), (599, True),
     (400, False), (401, False), (403, False), (404, False), (413, False)],
)
def test_is_retryable_status(status: int, retryable: bool) -> None:
    assert is_retryable_status(status) is retryable


@pytest.mark.parametrize(
    ("exc_type", "retryable"),
    [
        # A dropped SSE stream ("peer closed connection without sending
        # complete message body") must stay retryable or the runner's
        # restart_on_partial recovery never engages.
        (httpx.RemoteProtocolError, True),
        (httpx.ReadTimeout, True),
        (httpx.ConnectError, True),
        (httpx.ReadError, True),
        (httpx.LocalProtocolError, False),
        (httpx.UnsupportedProtocol, False),
    ],
)
def test_transport_error_retry_classification(
    exc_type: type[httpx.TransportError], retryable: bool
) -> None:
    with pytest.raises(ProviderError) as exc_info:
        raise_for_transport_error(
            exc_type("boom"), vendor="fake", model="m1", label="Fake"
        )

    assert exc_info.value.retryable is retryable


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("api.openai.com", True),
        ("eu.api.openai.com", True),  # regional data-residency host
        ("a.b.api.openai.com", True),
        # Lookalikes must not match.
        ("evilapi.openai.com", False),
        ("api.openai.com.evil.test", False),
        ("openai.com", False),
        ("example.test", False),
        ("", False),
        (None, False),
    ],
)
def test_host_matches(host: str | None, expected: bool) -> None:
    assert host_matches(host, ("api.openai.com",)) is expected


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
