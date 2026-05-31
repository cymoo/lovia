"""Tests for the OpenAI Chat Completions provider adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from lovia import Agent, ImageBlock, Runner, TextBlock, events
from lovia.exceptions import ContextOverflowError, ProviderError
from lovia.items import (
    FinishDelta,
    InputMessageItem,
    ItemDelta,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
from lovia.messages import ChatMessage, ToolCall
from lovia.providers.base import ModelSettings
from lovia.providers.openai_chat import (
    OpenAIChatProvider,
    _OPENAI_CONTEXT_WINDOWS,
    _is_context_overflow,
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


async def _collect(stream: Any) -> list[ItemDelta]:
    out: list[ItemDelta] = []
    async for delta in stream:
        out.append(delta)
    return out


def _deltas(deltas: list[ItemDelta], cls: type[Any]) -> Iterator[Any]:
    return (delta for delta in deltas if isinstance(delta, cls))


def test_message_to_openai_serializes_multimodal_and_tool_fields() -> None:
    msg = ChatMessage(
        role="assistant",
        content=[
            TextBlock("look"),
            ImageBlock(url="https://example.test/img.png", detail="low"),
        ],
        tool_calls=[ToolCall(id="c1", name="lookup", arguments='{"q":"x"}')],
        name="agent",
        reasoning_content="thinking",
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
        "reasoning_content": "thinking",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q":"x"}'},
            }
        ],
        "name": "agent",
    }


def test_build_payload_maps_settings_and_stream_options() -> None:
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test")

    payload = provider._build_payload(
        [ChatMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "f"}}],
        response_format={"type": "json_object"},
        settings=ModelSettings(
            temperature=0,
            top_p=0.5,
            max_tokens=50,
            stop=["END"],
            parallel_tool_calls=False,
            extra={"seed": 1},
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
            [InputMessageItem(role="user", content="hi")],
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
        await _collect(provider.stream([InputMessageItem(role="user", content="hi")]))

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
        await _collect(provider.stream([InputMessageItem(role="user", content="hi")]))


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


def test_context_window_table_covers_current_openai_models() -> None:
    expected = {
        "gpt-5.5": 1_050_000,
        "gpt-5.5-pro": 1_050_000,
        "gpt-5.4": 1_050_000,
        "gpt-5.4-mini": 400_000,
        "gpt-5.3-codex": 400_000,
        "gpt-5.2": 400_000,
        "gpt-5.2-codex": 400_000,
        "gpt-5.1": 400_000,
        "gpt-5.1-codex": 400_000,
        "gpt-5-codex": 400_000,
        "gpt-4.1": 1_047_576,
        "gpt-4.1-mini": 1_047_576,
        "gpt-4.1-nano": 1_047_576,
        "o3": 200_000,
        "o4-mini": 200_000,
    }

    provider = OpenAIChatProvider(model="gpt-5.5", api_key="sk-test")

    for model, window in expected.items():
        assert _OPENAI_CONTEXT_WINDOWS[model] == window
        assert provider.context_window(model) == window
