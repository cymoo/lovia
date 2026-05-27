"""Tests for the OpenAI Responses provider adapter.

Uses a mocked ``httpx`` transport to feed the adapter a canned SSE event
stream and asserts that the resulting :class:`ItemDelta` sequence matches
what the runner expects.
"""

from __future__ import annotations

import json

import httpx
import pytest

from lovia.items import (
    FinishDelta,
    InputMessageItem,
    ItemDelta,
    ReasoningDelta,
    ReasoningItem,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
from lovia.providers.openai_responses import (
    OpenAIResponsesProvider,
    _items_to_responses_input,
    _openai_chat_tool_to_responses,
)


def _sse(events: list[dict]) -> str:
    lines = []
    for evt in events:
        lines.append(f"event: {evt['type']}")
        lines.append(f"data: {json.dumps(evt)}")
        lines.append("")
    return "\n".join(lines) + "\n"


async def _collect(stream) -> list[ItemDelta]:
    out: list[ItemDelta] = []
    async for d in stream:
        out.append(d)
    return out


@pytest.mark.asyncio
async def test_responses_stream_parses_text_reasoning_and_function_call() -> None:
    body = _sse([
        {"type": "response.reasoning_summary_text.delta", "delta": "think"},
        {"type": "response.reasoning_summary_text.delta", "delta": "ing"},
        {"type": "response.output_text.delta", "delta": "hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
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
                "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
            },
        },
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIResponsesProvider(
        model="gpt-5", api_key="sk-test", client=client
    )

    deltas = await _collect(
        provider.stream([InputMessageItem(role="user", content="hi")])
    )

    text_deltas = [d for d in deltas if isinstance(d, TextDelta)]
    reasoning_deltas = [d for d in deltas if isinstance(d, ReasoningDelta)]
    tool_deltas = [d for d in deltas if isinstance(d, ToolCallDelta)]
    usage_deltas = [d for d in deltas if isinstance(d, UsageDelta)]
    finish_deltas = [d for d in deltas if isinstance(d, FinishDelta)]

    assert "".join(d.text for d in text_deltas) == "hello"
    assert "".join(d.text for d in reasoning_deltas) == "thinking"
    assert [d.arguments for d in tool_deltas] == ['{"x":', "1}"]
    assert all(d.call_id == "c1" and d.name == "do_thing" for d in tool_deltas)
    assert usage_deltas[0].usage.total_tokens == 12
    assert finish_deltas[0].reason == "completed"


def test_items_to_responses_input_preserves_reasoning_and_function_calls() -> None:
    items = [
        InputMessageItem(role="system", content="be concise"),
        InputMessageItem(role="user", content="2+2?"),
        ReasoningItem(id="rs_1", content="enc-blob"),
    ]
    out = _items_to_responses_input(items)
    assert out[0] == {
        "type": "message",
        "role": "system",
        "content": [{"type": "input_text", "text": "be concise"}],
    }
    assert out[1]["role"] == "user"
    assert out[2] == {
        "type": "reasoning",
        "summary": [],
        "id": "rs_1",
        "encrypted_content": "enc-blob",
    }


def test_openai_chat_tool_to_responses_flattens_function_shape() -> None:
    chat_tool = {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look something up",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    out = _openai_chat_tool_to_responses(chat_tool)
    assert out == {
        "type": "function",
        "name": "lookup",
        "description": "Look something up",
        "parameters": {"type": "object", "properties": {}},
    }
