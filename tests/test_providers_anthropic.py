"""Tests for the Anthropic provider adapter."""

from __future__ import annotations

import httpx

from lovia import ImageBlock, TextBlock
from lovia.messages import ChatMessage, ToolCall
from lovia.providers.anthropic import (
    AnthropicProvider,
    _openai_tool_to_anthropic,
    _to_anthropic_messages,
)
from lovia.providers.base import ModelSettings


# ---------- Message / tool translation ----------


def test_message_translation_extracts_system_and_tool_blocks() -> None:
    msgs = [
        ChatMessage(role="system", content="be terse"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="c1", name="add", arguments='{"a":1,"b":2}')],
        ),
        ChatMessage(role="tool", content="3", tool_call_id="c1"),
        ChatMessage(role="user", content="thanks"),
    ]
    system, out = _to_anthropic_messages(msgs)
    assert system == [{"type": "text", "text": "be terse"}]
    assert out[0] == {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    blocks = out[1]["content"]
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["input"] == {"a": 1, "b": 2}
    assert out[2]["content"][0]["type"] == "tool_result"
    assert out[2]["content"][0]["tool_use_id"] == "c1"


def test_tool_schema_translation() -> None:
    schema = {
        "type": "function",
        "function": {
            "name": "add",
            "description": "sum",
            "parameters": {"type": "object", "properties": {"a": {"type": "integer"}}},
        },
    }
    out = _openai_tool_to_anthropic(schema)
    assert out["name"] == "add"
    assert out["input_schema"]["properties"]["a"]["type"] == "integer"


# ---------- Image block translation ----------


def test_translates_image_blocks_with_url_and_base64() -> None:
    msgs = [
        ChatMessage(
            role="user",
            content=[
                TextBlock("describe"),
                ImageBlock(url="https://x/y.png"),
                ImageBlock(data="ZmFrZQ==", mime_type="image/png"),
            ],
        )
    ]
    _, out = _to_anthropic_messages(msgs)
    parts = out[0]["content"]
    assert parts[0] == {"type": "text", "text": "describe"}
    assert parts[1] == {
        "type": "image",
        "source": {"type": "url", "url": "https://x/y.png"},
    }
    assert parts[2]["type"] == "image"
    assert parts[2]["source"]["type"] == "base64"
    assert parts[2]["source"]["media_type"] == "image/png"


# ---------- Response parsing ----------
# Non-streaming response parsing was removed alongside ``Provider.generate``;
# the streaming path is exercised end-to-end via the runner tests.


# ---------- cache_control on system + tools ----------


def test_cache_control_inserted_when_cache_system_true() -> None:
    provider = AnthropicProvider(
        model="claude-3-haiku-20240307",
        api_key="x",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ),
    )
    payload = provider._build_payload(
        messages=[
            ChatMessage(role="system", content="be terse"),
            ChatMessage(role="user", content="hi"),
        ],
        tools=[
            {
                "type": "function",
                "function": {"name": "f", "parameters": {"type": "object"}},
            }
        ],
        settings=ModelSettings(cache_system=True),
        stream=False,
    )
    assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_cache_control_omitted_by_default() -> None:
    provider = AnthropicProvider(
        model="claude-3-haiku-20240307",
        api_key="x",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ),
    )
    payload = provider._build_payload(
        messages=[
            ChatMessage(role="system", content="be terse"),
            ChatMessage(role="user", content="hi"),
        ],
        tools=None,
        settings=ModelSettings(),
        stream=False,
    )
    assert "cache_control" not in payload["system"][-1]
