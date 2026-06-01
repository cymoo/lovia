"""Tests for the OpenAI Responses provider adapter."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from lovia.exceptions import ContextOverflowError, ProviderError
from lovia.items import (
    FinishDelta,
    ItemCompletedDelta,
    InputMessageItem,
    ItemDelta,
    MessageOutputItem,
    ReasoningDelta,
    ReasoningItem,
    TextDelta,
    ToolCallDelta,
    ToolCallItem,
    ToolCallOutputItem,
    UsageDelta,
)
from lovia.providers.base import ModelSettings
from lovia.providers.openai_responses import (
    OpenAIResponsesProvider,
    _items_to_responses_input,
    _openai_chat_tool_to_responses,
)


def _sse(events: list[dict[str, Any]]) -> bytes:
    lines: list[str] = []
    for evt in events:
        lines.append(f"event: {evt['type']}")
        lines.append(f"data: {json.dumps(evt)}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


async def _collect(stream: Any) -> list[ItemDelta]:
    out: list[ItemDelta] = []
    async for delta in stream:
        out.append(delta)
    return out


def _deltas(deltas: list[ItemDelta], cls: type[Any]) -> Iterator[Any]:
    return (delta for delta in deltas if isinstance(delta, cls))


def test_items_to_responses_input_preserves_all_item_shapes() -> None:
    items = [
        InputMessageItem(role="system", content="be concise"),
        InputMessageItem(role="user", content="2+2?"),
        MessageOutputItem(id="msg_1", content="4"),
        ReasoningItem(
            id="rs_1",
            content="summary",
            provider="openai-responses",
            metadata={"encrypted_content": "enc-blob"},
        ),
        ReasoningItem(id="other", content="ignored", provider="anthropic"),
        ToolCallItem(call_id="call_1", name="lookup", arguments='{"q":"x"}'),
        ToolCallOutputItem(call_id="call_1", output="ok"),
    ]

    out = _items_to_responses_input(items)

    assert out == [
        {
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": "be concise"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "2+2?"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "4"}],
            "id": "msg_1",
        },
        {
            "type": "reasoning",
            "summary": [],
            "id": "rs_1",
            "encrypted_content": "enc-blob",
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"q":"x"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "ok",
        },
    ]


def test_openai_chat_tool_to_responses_flattens_function_shape() -> None:
    chat_tool = {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look something up",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    assert _openai_chat_tool_to_responses(chat_tool) == {
        "type": "function",
        "name": "lookup",
        "description": "Look something up",
        "parameters": {"type": "object", "properties": {}},
    }


def test_build_payload_maps_settings_tools_and_structured_output() -> None:
    provider = OpenAIResponsesProvider(model="gpt-5", api_key="sk-test")
    payload = provider._build_payload(
        [InputMessageItem(role="user", content="hi")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "Answer",
                "schema": {"type": "object"},
                "strict": True,
            },
        },
        settings=ModelSettings(
            temperature=0,
            top_p=0.5,
            max_tokens=100,
            parallel_tool_calls=False,
            provider_options={"openai-responses": {"previous_response_id": "resp_1"}},
        ),
        stream=True,
    )

    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    assert payload["text"] == {
        "format": {
            "type": "json_schema",
            "name": "Answer",
            "schema": {"type": "object"},
            "strict": True,
        }
    }
    assert payload["temperature"] == 0
    assert payload["top_p"] == 0.5
    assert payload["max_output_tokens"] == 100
    assert payload["parallel_tool_calls"] is False
    assert payload["previous_response_id"] == "resp_1"


@pytest.mark.asyncio
async def test_responses_stream_parses_text_reasoning_tool_usage_and_finish() -> None:
    body = _sse(
        [
            {"type": "response.reasoning_summary_text.delta", "delta": "think"},
            {"type": "response.reasoning_text.delta", "delta": "ing"},
            {"type": "response.output_text.delta", "delta": "hel"},
            {"type": "response.output_text.delta", "delta": "lo"},
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"text": "thinking"}],
                    "encrypted_content": "enc",
                },
            },
            {
                "type": "response.output_item.done",
                "output_index": 2,
                "item": {
                    "type": "message",
                    "id": "msg_1",
                    "content": [{"type": "output_text", "text": "hello"}],
                },
            },
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"type": "function_call", "call_id": "c1", "name": "do_thing"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "delta": '{"x":',
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "delta": "1}",
            },
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 7,
                        "input_tokens_details": {"cached_tokens": 3},
                    },
                },
            },
        ]
    )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = OpenAIResponsesProvider(model="gpt-5", api_key="sk-test", client=client)

    deltas = await _collect(
        provider.stream([InputMessageItem(role="user", content="hi")])
    )

    assert "".join(delta.text for delta in _deltas(deltas, TextDelta)) == "hello"
    assert (
        "".join(delta.text for delta in _deltas(deltas, ReasoningDelta)) == "thinking"
    )
    tool_deltas = list(_deltas(deltas, ToolCallDelta))
    assert [delta.arguments for delta in tool_deltas] == ["", '{"x":', "1}"]
    assert all(
        delta.call_id == "c1" and delta.name == "do_thing" for delta in tool_deltas
    )
    usage = next(_deltas(deltas, UsageDelta)).usage
    assert usage.input_tokens == 5
    assert usage.output_tokens == 7
    assert usage.cache_read_tokens == 3
    completed_items = [delta.item for delta in _deltas(deltas, ItemCompletedDelta)]
    assert completed_items[:2] == [
        ReasoningItem(
            id="rs_1",
            content="thinking",
            provider="openai-responses",
            metadata={"encrypted_content": "enc"},
        ),
        MessageOutputItem(id="msg_1", content="hello"),
    ]
    assert next(_deltas(deltas, FinishDelta)).reason == "completed"


@pytest.mark.asyncio
async def test_responses_stream_preserves_zero_argument_function_call() -> None:
    body = _sse(
        [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "function_call", "call_id": "c1", "name": "ping"},
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = OpenAIResponsesProvider(model="gpt-5", api_key="sk-test", client=client)

    deltas = await _collect(
        provider.stream([InputMessageItem(role="user", content="call ping")])
    )

    tool_delta = next(_deltas(deltas, ToolCallDelta))
    assert tool_delta.call_id == "c1"
    assert tool_delta.name == "ping"
    assert tool_delta.arguments == ""


@pytest.mark.asyncio
async def test_responses_stream_uses_done_arguments_when_deltas_are_absent() -> None:
    body = _sse(
        [
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "lookup",
                    "arguments": '{"q":"x"}',
                },
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = OpenAIResponsesProvider(model="gpt-5", api_key="sk-test", client=client)

    deltas = await _collect(
        provider.stream([InputMessageItem(role="user", content="call lookup")])
    )

    tool_deltas = list(_deltas(deltas, ToolCallDelta))
    assert [delta.arguments for delta in tool_deltas] == ["", '{"q":"x"}']
    assert all(
        delta.call_id == "c1" and delta.name == "lookup" for delta in tool_deltas
    )


@pytest.mark.asyncio
async def test_responses_stream_does_not_duplicate_done_arguments_after_deltas() -> (
    None
):
    body = _sse(
        [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "function_call", "call_id": "c1", "name": "lookup"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "delta": '{"q":',
            },
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "arguments": '{"q":"x"}',
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = OpenAIResponsesProvider(model="gpt-5", api_key="sk-test", client=client)

    deltas = await _collect(
        provider.stream([InputMessageItem(role="user", content="call lookup")])
    )

    assert [delta.arguments for delta in _deltas(deltas, ToolCallDelta)] == [
        "",
        '{"q":',
        '"x"}',
    ]


@pytest.mark.asyncio
async def test_responses_stream_error_raises_provider_error() -> None:
    body = _sse(
        [
            {
                "type": "error",
                "error": {"type": "overloaded_error", "message": "try later"},
            }
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = OpenAIResponsesProvider(model="gpt-5", api_key="sk-test", client=client)

    with pytest.raises(ProviderError) as exc_info:
        await _collect(provider.stream([InputMessageItem(role="user", content="hi")]))

    assert exc_info.value.vendor == "openai"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_responses_http_errors_are_classified() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400,
                content=b'{"error":{"code":"context_length_exceeded"}}',
            )
        )
    )
    provider = OpenAIResponsesProvider(model="gpt-5", api_key="sk-test", client=client)

    with pytest.raises(ContextOverflowError):
        await _collect(provider.stream([InputMessageItem(role="user", content="hi")]))


@pytest.mark.asyncio
async def test_responses_provider_closes_owned_client() -> None:
    provider = OpenAIResponsesProvider(model="gpt-5", api_key="sk-test")

    await provider.aclose()

    assert provider._client.is_closed
