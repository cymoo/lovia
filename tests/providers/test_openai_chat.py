"""Tests for the OpenAI Chat Completions provider adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from lovia import Agent, FilePart, ImagePart, Runner, TextPart, events
from lovia.exceptions import ContextOverflowError, ProviderError, UserError
from lovia.transcript import (
    FinishDelta,
    EntryCompletedDelta,
    InputEntry,
    ModelDelta,
    AssistantTextEntry,
    ReasoningDelta,
    ReasoningEntry,
    TextDelta,
    ToolCallEntry,
    ToolCallDelta,
    ToolResultEntry,
    UsageDelta,
)
from lovia.messages import Message, ToolCall
from lovia.providers.base import ModelSettings
from lovia.providers.openai_chat import (
    OpenAIChatProvider,
    _is_context_overflow,
    entries_to_openai_messages,
    message_to_openai,
)

from tests.scripted_provider import ScriptedProvider, text


def _sse(events: list[dict[str, Any]]) -> bytes:
    lines: list[str] = []
    for evt in events:
        lines.append("event: chat.completion.chunk")
        lines.append(f"data: {json.dumps(evt)}")
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines).encode()


async def _collect(stream: Any) -> list[ModelDelta]:
    out: list[ModelDelta] = []
    async for delta in stream:
        out.append(delta)
    return out


def _deltas(deltas: list[ModelDelta], cls: type[Any]) -> Iterator[Any]:
    return (delta for delta in deltas if isinstance(delta, cls))


def test_message_to_openai_serializes_multimodal_and_tool_fields() -> None:
    msg = Message(
        role="assistant",
        content=[
            TextPart("look"),
            ImagePart(url="https://example.test/img.png", detail="low"),
        ],
        tool_calls=[ToolCall(id="c1", name="lookup", arguments='{"q":"x"}')],
        name="agent",
    )

    out = message_to_openai(msg)

    assert out == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "look"},
            {
                "type": "image_url",
                "image_url": {"url": "https://example.test/img.png", "detail": "low"},
            },
        ],
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q":"x"}'},
            }
        ],
        "name": "agent",
    }


def test_message_to_openai_serializes_inline_file_blocks() -> None:
    msg = Message(
        role="user",
        content=[
            TextPart("summarize"),
            FilePart(data="cGRm", mime_type="application/pdf", filename="doc.pdf"),
        ],
    )

    out = message_to_openai(msg)

    assert out["content"] == [
        {"type": "text", "text": "summarize"},
        {"type": "file", "file": {"file_data": "cGRm", "filename": "doc.pdf"}},
    ]


def test_message_to_openai_serializes_tool_result_messages() -> None:
    msg = Message(role="tool", content="42", tool_call_id="call_1", name="ignored")

    assert message_to_openai(msg) == {
        "role": "tool",
        "content": "42",
        "tool_call_id": "call_1",
    }


def test_message_to_openai_rejects_file_url_blocks() -> None:
    msg = Message(
        role="user",
        content=[FilePart.from_url("https://example.test/doc.pdf")],
    )

    with pytest.raises(UserError, match="does not support FilePart URL"):
        message_to_openai(msg)


def test_entries_to_openai_messages_flushes_assistant_entries_in_order() -> None:
    out = entries_to_openai_messages(
        [
            InputEntry(role="user", content="first"),
            ReasoningEntry(content="ignored", provider="other-provider"),
            ReasoningEntry(content="think", provider="openai-chat"),
            AssistantTextEntry(content="hel"),
            AssistantTextEntry(content="lo"),
            ToolCallEntry(call_id="call_1", name="add", arguments='{"a":1}'),
            ToolResultEntry(call_id="call_1", output="1"),
            AssistantTextEntry(content="done"),
            InputEntry(role="user", content="next"),
        ]
    )

    assert out == [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": "hello",
            "reasoning_content": "think",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a":1}'},
                }
            ],
        },
        {"role": "tool", "content": "1", "tool_call_id": "call_1"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "next"},
    ]


def test_build_payload_maps_settings_and_stream_options() -> None:
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test")

    payload = provider._build_payload(
        [InputEntry(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "f"}}],
        response_format={"type": "json_object"},
        settings=ModelSettings(
            temperature=0,
            top_p=0.5,
            max_tokens=50,
            stop=["END"],
            parallel_tool_calls=False,
            provider_options={"openai-chat": {"seed": 1}},
        ),
        stream=True,
    )

    assert payload["model"] == "gpt-5"
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["tools"] == [{"type": "function", "function": {"name": "f"}}]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0
    assert payload["top_p"] == 0.5
    assert payload["max_tokens"] == 50
    assert payload["stop"] == ["END"]
    assert payload["parallel_tool_calls"] is False
    assert payload["seed"] == 1


def test_headers_keep_explicit_api_key_when_extra_headers_overlap() -> None:
    provider = OpenAIChatProvider(
        model="gpt-5",
        api_key="real-key",
        default_headers={"Authorization": "Bearer wrong-key", "X-Test": "1"},
    )

    headers = provider._headers()

    assert headers["Authorization"] == "Bearer real-key"
    assert headers["X-Test"] == "1"


@pytest.mark.asyncio
async def test_created_client_ignores_ambient_socks_proxy(monkeypatch: Any) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7897")
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test")

    provider._http()
    await provider.aclose()


@pytest.mark.asyncio
async def test_chat_stream_parses_text_reasoning_tool_usage_and_finish() -> None:
    captured: dict[str, Any] = {}
    body = _sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "content": "hel",
                            "reasoning_content": "think",
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "content": "lo",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"x"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
                "choices": [],
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    deltas = await _collect(
        provider.stream(
            [InputEntry(role="user", content="hi")],
            settings=ModelSettings(max_tokens=10),
        )
    )

    assert captured["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert "".join(delta.text for delta in _deltas(deltas, TextDelta)) == "hello"
    assert "".join(delta.text for delta in _deltas(deltas, ReasoningDelta)) == "think"
    tool_deltas = list(_deltas(deltas, ToolCallDelta))
    assert [delta.arguments for delta in tool_deltas] == ['{"q":', '"x"}']
    assert all(
        delta.call_id == "c1" and delta.name == "lookup" for delta in tool_deltas
    )
    usage = next(_deltas(deltas, UsageDelta)).usage
    assert usage.input_tokens == 9
    assert usage.output_tokens == 4
    assert usage.cache_read_tokens == 2
    entry = next(_deltas(deltas, EntryCompletedDelta)).entry
    assert isinstance(entry, ReasoningEntry)
    assert entry.content == "think"
    assert entry.provider == "openai-chat"
    assert next(_deltas(deltas, FinishDelta)).reason == "tool_calls"


@pytest.mark.asyncio
async def test_chat_http_errors_are_classified() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, content=b"rate limited")
        )
    )
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    with pytest.raises(ProviderError) as exc_info:
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    assert exc_info.value.vendor == "openai"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_chat_missing_official_api_key_raises_user_error(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIChatProvider(model="gpt-5", base_url="https://api.openai.com/v1")

    with pytest.raises(UserError, match="requires an API key"):
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))


@pytest.mark.asyncio
async def test_chat_transport_errors_are_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    with pytest.raises(ProviderError) as exc_info:
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    assert exc_info.value.vendor == "openai"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_chat_context_overflow_is_classified() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400,
                content=b'{"error":{"code":"context_length_exceeded"}}',
            )
        )
    )
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    with pytest.raises(ContextOverflowError):
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))


def test_supports_json_schema_defaults_to_official_openai_only() -> None:
    assert OpenAIChatProvider(
        model="gpt-5", base_url="https://api.openai.com/v1"
    ).supports_json_schema
    assert not OpenAIChatProvider(
        model="gpt-5", base_url="https://example.test/v1"
    ).supports_json_schema
    assert OpenAIChatProvider(
        model="gpt-5",
        base_url="https://example.test/v1",
        supports_json_schema=True,
    ).supports_json_schema


def test_reasoning_delta_event_emitted_by_runner() -> None:
    provider = ScriptedProvider([text("done.", reasoning="thinking...")])
    agent = Agent(name="a", model=provider)

    async def go() -> tuple[str, str]:
        handle = Runner.stream(agent, "hi")
        text_d: list[str] = []
        reasoning_d: list[str] = []
        async for ev in handle:
            if isinstance(ev, events.TextDelta):
                text_d.append(ev.delta)
            elif isinstance(ev, events.ReasoningDelta):
                reasoning_d.append(ev.delta)
        return "".join(text_d), "".join(reasoning_d)

    text_out, reasoning_out = asyncio.run(go())
    assert text_out == "done."
    assert reasoning_out == "thinking..."


def test_is_context_overflow_classic_openai_code() -> None:
    body = '{"error":{"code":"context_length_exceeded","message":"..."}}'
    assert _is_context_overflow(400, body)


def test_is_context_overflow_modal_style_input_tokens_400() -> None:
    body = (
        '{"error":{"message":"You passed 131073 input tokens and requested '
        "0 output tokens. However, the model's context length is only "
        "131072 tokens, resulting in a maximum input length of 131072 "
        "tokens. Please reduce the length of the input prompt. "
        '(parameter=input_tokens, value=131073)","type":"BadRequestError",'
        '"param":"input_tokens","code":400}}'
    )
    assert _is_context_overflow(400, body)


def test_is_context_overflow_anthropic_style_413() -> None:
    assert _is_context_overflow(413, "request too large for the model")


def test_is_context_overflow_ignores_other_errors() -> None:
    assert not _is_context_overflow(400, "invalid api key")
    assert not _is_context_overflow(500, "context_length_exceeded")


def test_is_context_overflow_string_too_long_only_with_context() -> None:
    assert _is_context_overflow(400, "string too long; max context exceeded")
    assert not _is_context_overflow(400, "string too long: tool argument")
