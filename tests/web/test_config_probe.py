"""Tests for endpoint probing (``lovia.web.config.probe``)."""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("fastapi")

from lovia.web.config import (  # noqa: E402
    Connection,
    ModelProfile,
    ValidationOutcome,
    unlisted_model_note,
    validate_connection,
)


def _conn(model: str = "deepseek-v4-pro", **overrides: object) -> Connection:
    conn = Connection.from_profile(ModelProfile(id="probe", model=model))
    for key, value in overrides.items():
        setattr(conn, key, value)
    return conn


def test_validate_ok_and_openai_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    conn = _conn(base_url="http://gw/v1", api_key="sk-1")
    outcome, _ = validate_connection(conn, transport=httpx.MockTransport(handler))
    assert outcome is ValidationOutcome.OK
    assert str(seen[0].url) == "http://gw/v1/models"
    assert seen[0].headers["authorization"] == "Bearer sk-1"


def test_validate_anthropic_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    conn = _conn(model="anthropic:claude-x", base_url="http://gw/v1", api_key="sk-2")
    validate_connection(conn, transport=httpx.MockTransport(handler))
    assert seen[0].headers["x-api-key"] == "sk-2"
    assert "anthropic-version" in seen[0].headers


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (401, ValidationOutcome.AUTH_FAILED),
        (403, ValidationOutcome.AUTH_FAILED),
        (404, ValidationOutcome.UNVERIFIABLE),
        (500, ValidationOutcome.UNVERIFIABLE),
    ],
)
def test_validate_status_classification(
    status: int, outcome: ValidationOutcome
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    got, detail = validate_connection(_conn(), transport=transport)
    assert got is outcome
    assert str(status) in detail


def test_validate_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns boom")

    got, detail = validate_connection(_conn(), transport=httpx.MockTransport(handler))
    assert got is ValidationOutcome.UNREACHABLE
    assert "dns boom" in detail


# ------------------------------------- context window reported by /models -


def _models_transport(*entries: dict) -> httpx.MockTransport:
    payload = {"object": "list", "data": list(entries)}
    return httpx.MockTransport(lambda request: httpx.Response(200, json=payload))


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"id": "deepseek-v4-pro", "max_model_len": 32_768}, 32_768),  # vLLM/SGLang
        ({"id": "deepseek-v4-pro", "context_window": 131_072}, 131_072),  # Groq
        ({"id": "deepseek-v4-pro", "context_length": 8_192}, 8_192),  # Together
        (
            {  # OpenRouter: the routed provider's limit beats the model-level one
                "id": "deepseek-v4-pro",
                "context_length": 1_000_000,
                "top_provider": {"context_length": 64_000},
            },
            64_000,
        ),
    ],
)
def test_validate_adopts_the_window_the_endpoint_reports(
    entry: dict, expected: int
) -> None:
    conn = _conn(base_url="http://gw/v1", api_key="sk-1")
    outcome, _ = validate_connection(conn, transport=_models_transport(entry))
    assert outcome is ValidationOutcome.OK
    assert conn.context_window == expected
    # Marked as a deployment fact: used for this launch, never persisted.
    assert conn.window_from_endpoint is True


@pytest.mark.parametrize(
    "transport",
    [
        # The official OpenAI/Anthropic/DeepSeek shape publishes no window.
        _models_transport({"id": "deepseek-v4-pro", "owned_by": "deepseek"}),
        _models_transport({"id": "some-other-model", "max_model_len": 4096}),
        httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json")),
        httpx.MockTransport(lambda request: httpx.Response(200, json={"data": "nope"})),
    ],
)
def test_validate_leaves_the_window_unset_when_unreported(
    transport: httpx.MockTransport,
) -> None:
    conn = _conn(base_url="http://gw/v1", api_key="sk-1")
    outcome, _ = validate_connection(conn, transport=transport)
    assert outcome is ValidationOutcome.OK
    assert conn.context_window is None
    assert conn.window_from_endpoint is False


def test_validate_never_overrides_a_configured_window() -> None:
    conn = _conn(base_url="http://gw/v1", api_key="sk-1", context_window=111_111)
    validate_connection(
        conn,
        transport=_models_transport({"id": "deepseek-v4-pro", "max_model_len": 4096}),
    )
    assert conn.context_window == 111_111
    assert conn.window_from_endpoint is False


# ------------------------------------------------------- unlisted models -


def _listing_transport(*ids: str) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": i} for i in ids]})
    )


def test_unlisted_model_gets_a_note_with_suggestions() -> None:
    conn = _conn(model="openai:gpt-5.6", api_key="sk-x")
    validate_connection(conn, transport=_listing_transport("gpt-5.5", "gpt-5.5-mini"))
    note = unlisted_model_note(conn)
    assert note is not None
    assert "does not list 'gpt-5.6'" in note
    assert "close: gpt-5.5" in note


def test_listed_model_has_no_note() -> None:
    conn = _conn(model="openai:gpt-5.5", api_key="sk-x")
    validate_connection(conn, transport=_listing_transport("gpt-5.5"))
    assert unlisted_model_note(conn) is None


def test_no_listing_has_no_note() -> None:
    conn = _conn()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    validate_connection(conn, transport=transport)
    assert unlisted_model_note(conn) is None
