"""Tests for the Anthropic provider adapter."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from lovia import FilePart, ImagePart, TextPart
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
    ToolCallDelta,
    ToolCallEntry,
    ToolResultEntry,
    UsageDelta,
)
from lovia.providers.anthropic import (
    AnthropicProvider,
    _is_context_overflow,
    _normalize_stop_reason,
    _openai_tool_to_anthropic,
    _to_anthropic_messages,
)
from lovia.providers.base import ModelSettings


def _sse(events: list[dict[str, Any]]) -> bytes:
    lines: list[str] = []
    for evt in events:
        lines.append(f"event: {evt['type']}")
        lines.append(f"data: {json.dumps(evt)}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


async def _collect(stream: Any) -> list[ModelDelta]:
    out: list[ModelDelta] = []
    async for delta in stream:
        out.append(delta)
    return out


def _deltas(deltas: list[ModelDelta], cls: type[Any]) -> Iterator[Any]:
    return (delta for delta in deltas if isinstance(delta, cls))


def test_message_translation_extracts_system_and_tool_blocks() -> None:
    entries = [
        InputEntry(role="system", content="be terse"),
        InputEntry(role="system", content=[TextPart("second")]),
        InputEntry(role="user", content="hi"),
        ReasoningEntry(
            content="thinking",
            provider="anthropic",
            metadata={"signature": "sig"},
        ),
        AssistantTextEntry(content="working"),
        ToolCallEntry(call_id="c1", name="add", arguments='{"a":1,"b":2}'),
        ToolResultEntry(call_id="c1", output="3"),
        InputEntry(role="user", content=""),
    ]

    system, out = _to_anthropic_messages(entries)

    assert system == [{"type": "text", "text": "be terse\n\nsecond"}]
    assert out[0] == {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    assistant_blocks = out[1]["content"]
    assert assistant_blocks[0] == {
        "type": "thinking",
        "thinking": "thinking",
        "signature": "sig",
    }
    assert assistant_blocks[1] == {"type": "text", "text": "working"}
    assert assistant_blocks[2]["type"] == "tool_use"
    assert assistant_blocks[2]["input"] == {"a": 1, "b": 2}
    # The trailing empty user entry contributes nothing (the API rejects
    # empty text blocks), leaving only the tool_result.
    assert out[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": "3"}
    ]


def test_message_translation_forwards_tool_result_is_error() -> None:
    _, out = _to_anthropic_messages(
        [
            ToolCallEntry(call_id="c1", name="add", arguments="{}"),
            ToolResultEntry(call_id="c1", output="boom", is_error=True),
        ]
    )

    assert out[1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "c1",
        "content": "boom",
        "is_error": True,
    }


def test_message_translation_skips_empty_content() -> None:
    _, out = _to_anthropic_messages(
        [
            InputEntry(role="system", content=[TextPart("")]),
            InputEntry(role="user", content=""),
            InputEntry(role="user", content=[TextPart(""), TextPart("hi")]),
            AssistantTextEntry(content=""),
            ToolCallEntry(call_id="c1", name="add", arguments="{}"),
        ]
    )

    system, _ = _to_anthropic_messages([InputEntry(role="system", content="")])
    assert system is None
    assert out[0] == {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    assert [block["type"] for block in out[1]["content"]] == ["tool_use"]


def test_message_translation_empty_user_between_assistant_turns() -> None:
    """An all-empty user entry must not split the surrounding assistant blocks."""
    _, out = _to_anthropic_messages(
        [
            InputEntry(role="user", content="go"),
            AssistantTextEntry(content="first"),
            InputEntry(role="user", content=""),
            AssistantTextEntry(content="second"),
        ]
    )

    assert [msg["role"] for msg in out] == ["user", "assistant"]
    assert [block["text"] for block in out[1]["content"]] == ["first", "second"]


def test_message_translation_keeps_non_text_parts_of_empty_text_message() -> None:
    _, out = _to_anthropic_messages(
        [
            InputEntry(
                role="user",
                content=[TextPart(""), ImagePart(url="https://x/y.png")],
            )
        ]
    )

    assert [block["type"] for block in out[0]["content"]] == ["image"]


def test_message_translation_wraps_invalid_tool_arguments() -> None:
    _, out = _to_anthropic_messages(
        [ToolCallEntry(call_id="c1", name="broken", arguments="{bad")]
    )

    assert out[0]["content"][0]["input"] == {"_raw": "{bad"}


def test_message_translation_drops_orphan_thinking() -> None:
    _, out = _to_anthropic_messages(
        [
            InputEntry(role="user", content="before"),
            ReasoningEntry(content="stale", provider="anthropic"),
            InputEntry(role="user", content="after"),
        ]
    )

    assert out == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "before"},
                {"type": "text", "text": "\n\n"},
                {"type": "text", "text": "after"},
            ],
        }
    ]


def test_message_translation_replays_redacted_thinking_with_tool_use() -> None:
    _, out = _to_anthropic_messages(
        [
            ReasoningEntry(
                content="", provider="anthropic", metadata={"redacted": "blob=="}
            ),
            ToolCallEntry(call_id="c1", name="add", arguments='{"a":1}'),
        ]
    )

    assert out[0]["content"][0] == {"type": "redacted_thinking", "data": "blob=="}
    assert out[0]["content"][1]["type"] == "tool_use"


def test_message_translation_drops_orphan_redacted_thinking() -> None:
    _, out = _to_anthropic_messages(
        [
            InputEntry(role="user", content="before"),
            ReasoningEntry(
                content="", provider="anthropic", metadata={"redacted": "blob=="}
            ),
            ReasoningEntry(content="stale", provider="anthropic"),
            InputEntry(role="user", content="after"),
        ]
    )

    assert all(
        block["type"] == "text" for msg in out for block in msg["content"]
    )


def test_message_translation_honors_reasoning_provider_param() -> None:
    entries: list[Any] = [
        ReasoningEntry(content="think", provider="my-anthropic"),
        ToolCallEntry(call_id="c1", name="add", arguments="{}"),
    ]

    _, default_out = _to_anthropic_messages(entries)
    _, custom_out = _to_anthropic_messages(entries, reasoning_provider="my-anthropic")

    assert default_out[0]["content"][0]["type"] == "tool_use"
    assert custom_out[0]["content"][0] == {"type": "thinking", "thinking": "think"}


def test_message_translation_replay_thinking_off_drops_reasoning() -> None:
    _, out = _to_anthropic_messages(
        [
            ReasoningEntry(content="think", provider="anthropic"),
            ReasoningEntry(
                content="", provider="anthropic", metadata={"redacted": "blob=="}
            ),
            ToolCallEntry(call_id="c1", name="add", arguments="{}"),
        ],
        replay_thinking=False,
    )

    assert [block["type"] for block in out[0]["content"]] == ["tool_use"]


def test_message_translation_keeps_thinking_with_tool_use() -> None:
    _, out = _to_anthropic_messages(
        [
            ReasoningEntry(content="think", provider="anthropic"),
            ToolCallEntry(call_id="c1", name="add", arguments='{"a":1}'),
        ]
    )

    assert out[0]["role"] == "assistant"
    assert out[0]["content"][0] == {"type": "thinking", "thinking": "think"}
    assert out[0]["content"][1]["type"] == "tool_use"


def test_tool_schema_translation_preserves_strict() -> None:
    schema = {
        "type": "function",
        "function": {
            "name": "add",
            "description": "sum",
            "parameters": {"type": "object", "properties": {"a": {"type": "integer"}}},
            "strict": True,
        },
    }

    out = _openai_tool_to_anthropic(schema)

    assert out["name"] == "add"
    assert out["description"] == "sum"
    assert out["input_schema"]["properties"]["a"]["type"] == "integer"
    assert out["strict"] is True


def test_translates_image_blocks_with_url_and_base64() -> None:
    msgs = [
        InputEntry(
            role="user",
            content=[
                TextPart("describe"),
                ImagePart(url="https://x/y.png"),
                ImagePart(data="ZmFrZQ==", mime_type="image/png"),
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


def test_translates_file_blocks_with_url_and_base64() -> None:
    msgs = [
        InputEntry(
            role="user",
            content=[
                FilePart.from_url("https://x/doc.pdf", filename="remote.pdf"),
                FilePart(data="cGRm", mime_type="application/pdf", filename="doc.pdf"),
            ],
        )
    ]

    _, out = _to_anthropic_messages(msgs)

    assert out[0]["content"] == [
        {
            "type": "document",
            "source": {"type": "url", "url": "https://x/doc.pdf"},
            "title": "remote.pdf",
        },
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": "cGRm",
            },
            "title": "doc.pdf",
        },
    ]


def test_translates_file_url_without_mime_type() -> None:
    msgs = [
        InputEntry(
            role="user",
            content=[FilePart.from_url("https://x/doc.pdf")],
        )
    ]

    _, out = _to_anthropic_messages(msgs)

    assert out[0]["content"] == [
        {
            "type": "document",
            "source": {"type": "url", "url": "https://x/doc.pdf"},
        }
    ]


def test_translates_text_file_block_as_plain_text_document() -> None:
    msgs = [
        InputEntry(
            role="user",
            content=[FilePart.from_bytes(b"hello", mime_type="text/plain")],
        )
    ]

    _, out = _to_anthropic_messages(msgs)

    assert out[0]["content"] == [
        {
            "type": "document",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": "hello",
            },
        }
    ]


def test_rejects_unsupported_anthropic_file_mime_type() -> None:
    msgs = [
        InputEntry(
            role="user",
            content=[FilePart(data="eA==", mime_type="application/octet-stream")],
        )
    ]

    with pytest.raises(UserError, match="support application/pdf or text/plain"):
        _to_anthropic_messages(msgs)


def test_rejects_text_file_block_with_invalid_utf8() -> None:
    msgs = [
        InputEntry(
            role="user",
            content=[FilePart.from_base64("//4=", mime_type="text/plain")],
        )
    ]

    with pytest.raises(UserError, match="valid UTF-8"):
        _to_anthropic_messages(msgs)


def test_rejects_non_pdf_anthropic_file_urls() -> None:
    msgs = [
        InputEntry(
            role="user",
            content=[FilePart.from_url("https://x/doc.txt", mime_type="text/plain")],
        )
    ]

    with pytest.raises(UserError, match="document URLs require application/pdf"):
        _to_anthropic_messages(msgs)


def test_build_payload_maps_settings_cache_and_structured_output() -> None:
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="x")

    payload = provider._build_payload(
        entries=[
            InputEntry(role="system", content="be terse"),
            InputEntry(role="user", content="hi"),
        ],
        tools=[
            {
                "type": "function",
                "function": {"name": "f", "parameters": {"type": "object"}},
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "Answer", "schema": {"type": "object"}},
        },
        settings=ModelSettings(
            temperature=0,
            top_p=0.8,
            max_tokens=0,
            stop=["END"],
            parallel_tool_calls=False,
            provider_options={"anthropic": {"cache_system": True}},
        ),
        stream=False,
    )

    assert payload["max_tokens"] == 0
    assert payload["temperature"] == 0
    assert payload["top_p"] == 0.8
    assert payload["stop_sequences"] == ["END"]
    assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}}
    }
    assert payload["tool_choice"] == {
        "type": "auto",
        "disable_parallel_tool_use": True,
    }


def test_build_payload_extra_overrides_adapter_defaults() -> None:
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="x")

    payload = provider._build_payload(
        entries=[InputEntry(role="user", content="hi")],
        tools=[
            {
                "type": "function",
                "function": {"name": "f", "parameters": {"type": "object"}},
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "Answer", "schema": {"type": "object"}},
        },
        settings=ModelSettings(
            parallel_tool_calls=False,
            provider_options={
                "anthropic": {
                    "output_config": {"format": {"type": "json_schema", "schema": {}}},
                    "tool_choice": {"type": "none"},
                }
            },
        ),
        stream=True,
    )

    assert payload["output_config"] == {"format": {"type": "json_schema", "schema": {}}}
    assert payload["tool_choice"] == {"type": "none"}
    assert payload["stream"] is True


def test_build_payload_gates_thinking_replay_by_endpoint_and_option() -> None:
    entries = [
        InputEntry(role="user", content="hi"),
        ReasoningEntry(
            content="think", provider="anthropic", metadata={"signature": "sig"}
        ),
        ToolCallEntry(call_id="c1", name="add", arguments="{}"),
        ToolResultEntry(call_id="c1", output="3"),
    ]

    def block_types(payload: dict) -> list[str]:
        return [
            block["type"]
            for message in payload["messages"]
            if message["role"] == "assistant"
            for block in message["content"]
        ]

    official = AnthropicProvider(
        model="claude-haiku-4-5",
        api_key="x",
        base_url="https://api.anthropic.com/v1",
    )
    compatible = AnthropicProvider(
        model="deepseek-v4-pro",
        api_key="x",
        base_url="https://api.deepseek.com/anthropic",
    )
    thinking_on = ModelSettings(
        provider_options={
            "anthropic": {"thinking": {"type": "enabled", "budget_tokens": 1024}}
        }
    )
    thinking_disabled = ModelSettings(
        provider_options={"anthropic": {"thinking": {"type": "disabled"}}}
    )

    def build(provider: AnthropicProvider, settings: ModelSettings | None) -> dict:
        return provider._build_payload(
            entries, tools=None, response_format=None, settings=settings, stream=True
        )

    # Official endpoint: thinking blocks are rejected unless thinking is on.
    assert block_types(build(official, None)) == ["tool_use"]
    assert block_types(build(official, thinking_disabled)) == ["tool_use"]
    assert block_types(build(official, thinking_on)) == ["thinking", "tool_use"]
    # Default-on endpoints replay regardless of the option.
    assert block_types(build(compatible, None)) == ["thinking", "tool_use"]


def test_response_format_ignores_unsupported_openai_shapes() -> None:
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="x")

    payload = provider._build_payload(
        entries=[InputEntry(role="user", content="hi")],
        tools=None,
        response_format={"type": "json_object"},
        settings=ModelSettings(),
        stream=False,
    )

    assert "output_config" not in payload


def test_headers_include_extra_headers_without_overriding_explicit_api_key() -> None:
    provider = AnthropicProvider(
        model="claude-haiku-4-5",
        api_key="real-key",
        default_headers={
            "anthropic-beta": "fine-grained-tool-streaming-2025-05-14",
            "x-api-key": "wrong-key",
        },
    )

    headers = provider._headers()

    assert headers["x-api-key"] == "real-key"
    assert headers["anthropic-beta"] == "fine-grained-tool-streaming-2025-05-14"


@pytest.mark.asyncio
async def test_created_client_ignores_ambient_socks_proxy(monkeypatch: Any) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7897")
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="x")

    provider._http()
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_parses_text_reasoning_tool_usage_and_finish() -> None:
    body = _sse(
        [
            {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 2,
                        "cache_read_input_tokens": 3,
                    }
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hi"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "thinking"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "thinking_delta", "thinking": "think"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "signature_delta", "signature": "sig"},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {"type": "tool_use", "id": "c1", "name": "lookup"},
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"q":'},
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '"x"}'},
            },
            {"type": "content_block_stop", "index": 2},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {
                    "output_tokens": 8,
                    "cache_creation_input_tokens": 4,
                    "cache_read_input_tokens": 5,
                },
            },
            {"type": "message_stop"},
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="x", client=client)

    deltas = await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    assert "".join(delta.text for delta in _deltas(deltas, TextDelta)) == "hi"
    assert "".join(delta.text for delta in _deltas(deltas, ReasoningDelta)) == "think"
    tool_deltas = list(_deltas(deltas, ToolCallDelta))
    assert [delta.arguments for delta in tool_deltas] == ["", '{"q":', '"x"}']
    assert all(
        delta.call_id == "c1" and delta.name == "lookup" for delta in tool_deltas
    )
    usage = next(_deltas(deltas, UsageDelta)).usage
    # ``input_tokens`` is normalized to the full prompt: Anthropic's raw
    # ``input_tokens`` (10, uncached slice only) plus the final cache
    # write/read counts from message_delta (4 + 5).
    assert usage.input_tokens == 19
    assert usage.output_tokens == 8
    assert usage.cache_write_tokens == 4
    assert usage.cache_read_tokens == 5
    completed_entries = [delta.entry for delta in _deltas(deltas, EntryCompletedDelta)]
    assert completed_entries[0] == AssistantTextEntry(content="hi")
    assert completed_entries[1] == ReasoningEntry(
        content="think",
        provider="anthropic",
        metadata={"signature": "sig"},
    )
    assert completed_entries[2] == ToolCallEntry(
        call_id="c1",
        name="lookup",
        arguments='{"q":"x"}',
    )
    assert next(_deltas(deltas, FinishDelta)).reason == "tool_calls"


@pytest.mark.asyncio
async def test_stream_captures_redacted_thinking() -> None:
    body = _sse(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "redacted_thinking", "data": "blob=="},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "hi"},
            },
            {"type": "content_block_stop", "index": 1},
            {"type": "message_stop"},
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="x", client=client)

    deltas = await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    # Redacted content is encrypted: preserved for replay, never displayed.
    assert not list(_deltas(deltas, ReasoningDelta))
    completed = [delta.entry for delta in _deltas(deltas, EntryCompletedDelta)]
    assert completed[0] == ReasoningEntry(
        content="", provider="anthropic", metadata={"redacted": "blob=="}
    )
    assert completed[1] == AssistantTextEntry(content="hi")


@pytest.mark.asyncio
async def test_stream_error_raises_provider_error() -> None:
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
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="x", client=client)

    with pytest.raises(ProviderError) as exc_info:
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    assert exc_info.value.vendor == "anthropic"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_missing_official_api_key_raises_user_error(monkeypatch: Any) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = AnthropicProvider(
        model="claude-haiku-4-5", base_url="https://api.anthropic.com/v1"
    )

    with pytest.raises(UserError, match="requires an API key"):
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))


@pytest.mark.asyncio
async def test_transport_errors_are_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="x", client=client)

    with pytest.raises(ProviderError) as exc_info:
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    assert exc_info.value.vendor == "anthropic"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_http_context_overflow_is_classified() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, content=b"prompt is too long")
        )
    )
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="x", client=client)

    with pytest.raises(ContextOverflowError):
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))


def test_stop_reason_and_context_overflow_helpers() -> None:
    assert _normalize_stop_reason("end_turn") == "stop"
    assert _normalize_stop_reason("max_tokens") == "length"
    assert _normalize_stop_reason("unknown") == "unknown"
    assert _is_context_overflow(400, "input is too long")
    assert _is_context_overflow(413, "context window exceeded")
    assert not _is_context_overflow(500, "prompt is too long")
    assert not _is_context_overflow(400, "invalid api key")


def test_context_window_includes_current_claude_aliases() -> None:
    provider = AnthropicProvider(model="claude-opus-4-8", api_key="x")

    # Default (non-beta) windows: the 1M variants require the ``context-1m``
    # beta header, which the adapter does not send.
    assert provider.context_window("claude-opus-4-8") == 200_000
    assert provider.context_window("claude-sonnet-4-6") == 200_000
    assert provider.context_window("claude-haiku-4-5") == 200_000
    assert provider.context_window("claude-sonnet-4-5-20250929") is None
